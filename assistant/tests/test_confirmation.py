"""확인 답변 AI 입구와 디스코드 버튼을 외부 연결 없이 검증한다."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from features import confirmation
from entrances.file_prompts import FilePromptView


class Meaning(unittest.TestCase):
    def test_model_reads_only_question_and_answer(self):
        for result in ('yes', 'no', 'unknown'):
            with patch('core.classifier.ask_system', return_value=('{"decision":"' + result + '"}', 0)) as model:
                self.assertEqual(confirmation.interpret('복사할까요?', '그래 진행해'), result)
                self.assertIn('복사할까요?', model.call_args.args[1])
                self.assertEqual(model.call_args.kwargs['timeout'], 15)

    def test_invalid_model_result_is_never_approval(self):
        for raw in ('yes', '[]', '{"decision":true}', '{"decision":"approve"}', '잘 모르겠어요'):
            with patch('core.classifier.ask_system', return_value=(raw, 0)):
                self.assertEqual(confirmation.interpret('삭제할까요?', '해도 되나?'), 'unknown')

    def test_invalid_input_does_not_call_model(self):
        with patch('core.classifier.ask_system') as model:
            for q, a in [('', '예'), ('q', ''), ('q' * 2001, '예'), ('q', 'a' * 501), (None, '예')]:
                with self.assertRaises(ValueError): confirmation.interpret(q, a)
            model.assert_not_called()


class Buttons(unittest.IsolatedAsyncioTestCase):
    def make(self, kind='confirm', destructive=False):
        cb = AsyncMock()
        prompt = {'id': 'qa', 'kind': kind, 'expiresAt': (time.time() + 30) * 1000, 'destructive': destructive, 'options': ['test/a', 'test/sub/a']}
        view = FilePromptView(prompt, 42, cb)
        view.message = SimpleNamespace(edit=AsyncMock())
        return view, cb

    async def test_button_order_color_and_choice(self):
        v, _ = self.make()
        self.assertEqual([b.label for b in v.children], ['예', '아니오'])
        self.assertEqual(v.children[0].style.value, 1)
        await v.close()
        v, _ = self.make(destructive=True)
        self.assertEqual([b.label for b in v.children], ['아니오', '예'])
        self.assertEqual(v.children[1].style.value, 4)
        await v.close()
        v, _ = self.make('choice')
        self.assertEqual([b.label for b in v.children], ['1', '2', '취소'])
        await v.close()

    async def test_only_author_once_and_timeout_removes_buttons(self):
        v, cb = self.make()
        foreign = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
        await v.clicked(foreign, '예'); cb.assert_not_awaited()
        own = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
        await v.clicked(own, '예'); await v.clicked(own, '예')
        cb.assert_awaited_once_with('예', 'qa')
        v.message.edit.assert_awaited_with(view=None)
        v, cb = self.make(); v.prompt['expiresAt'] = 0
        await v.clicked(own, '예'); cb.assert_not_awaited()
        await v.on_timeout(); v.message.edit.assert_awaited_with(view=None)

    async def test_real_bot_entry_sends_choice_then_confirmation_and_result(self):
        import importlib
        import discord
        from core import config
        prompt1 = {'id': 'select', 'kind': 'choice', 'expiresAt': (time.time() + 30) * 1000, 'options': ['test/a', 'test/sub/a']}
        prompt2 = {'id': 'confirm', 'kind': 'confirm', 'expiresAt': (time.time() + 30) * 1000, 'destructive': False}
        sent = []
        class Typing:
            async def __aenter__(self): pass
            async def __aexit__(self, *a): pass
        class Channel:
            id = 101
            parent_id = None
            name = '시험'
            def typing(self): return Typing()
            async def send(self, content, **kwargs):
                sent.append((content, kwargs))
                return SimpleNamespace(edit=AsyncMock())
        message = SimpleNamespace(author=SimpleNamespace(id=42, bot=False), channel=Channel(), guild=None, content='파일 복사 a 시험', attachments=[])
        with patch.object(config, 'DISCORD_TOKEN', 'qa-only'), patch.object(config, 'BOT_API_KEY', 'qa-only'), patch.object(discord.Client, 'run'):
            bot = importlib.import_module('entrances.discord_bot')
        bot._managed.clear()
        responses = [{'reply': '선택하세요', 'file_waiting': True, 'file_prompt': prompt1},
                     {'reply': '복사할까요?', 'file_waiting': True, 'file_prompt': prompt2},
                     {'reply': '복사했습니다.'}]
        interaction = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
        with patch.object(bot.api_client, 'send_detail', side_effect=responses) as api:
            await bot.on_message(message)
            select = sent[-1][1]['view']
            self.assertEqual([b.label for b in select.children], ['1', '2', '취소'])
            await select.children[1].callback(interaction)
            self.assertEqual(api.call_args.kwargs['file_prompt_id'], 'select')
            confirm = sent[-1][1]['view']
            self.assertEqual([b.label for b in confirm.children], ['예', '아니오'])
            await confirm.children[0].callback(interaction)
            self.assertEqual(api.call_args.kwargs['file_prompt_id'], 'confirm')
            self.assertEqual(sent[-1][0], '복사했습니다.')
            self.assertNotIn((101, 42), bot._file_prompt_views)
        await bot.client.close()
