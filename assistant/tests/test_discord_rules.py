"""디스코드 메시지에 답할지 정하는 규칙 (디스코드·네트워크 없음)"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core import config
from entrances import discord_rules as R

MINE, OTHERS, CHAT_CHANNEL, QUIET_CHANNEL = 1, 2, 30, 40


class Rules(unittest.TestCase):
    def setUp(self):
        self.real = (config.DISCORD_CHAT_GUILDS, config.DISCORD_CHAT_CHANNELS)
        config.DISCORD_CHAT_GUILDS, config.DISCORD_CHAT_CHANNELS = [str(MINE)], [str(CHAT_CHANNEL)]

    def tearDown(self):
        config.DISCORD_CHAT_GUILDS, config.DISCORD_CHAT_CHANNELS = self.real

    def test_dm_answers_everything(self):
        self.assertEqual(R.message_text(" 살아있나? ", True), "살아있나?")
        self.assertEqual(R.message_text("!상태", True), "!상태")
        self.assertIsNone(R.message_text("   ", True))

    def test_my_server_answers_every_channel(self):
        self.assertEqual(R.message_text("살아있나?", False, MINE, [QUIET_CHANNEL]), "살아있나?")

    def test_registered_channel_on_other_server(self):
        self.assertEqual(R.message_text("살아있나?", False, OTHERS, [CHAT_CHANNEL]), "살아있나?")

    def test_other_channel_needs_command(self):
        self.assertIsNone(R.message_text("살아있나?", False, OTHERS, [QUIET_CHANNEL]))
        self.assertEqual(R.message_text("!상태", False, OTHERS, [QUIET_CHANNEL]), "!상태")

    def test_thread_follows_parent_channel(self):
        self.assertEqual(R.message_text("안녕", False, OTHERS, [99, CHAT_CHANNEL]), "안녕")
        self.assertIsNone(R.message_text("안녕", False, OTHERS, [99, None]))

    def test_mention_is_removed_in_chat_place_only(self):
        self.assertEqual(R.message_text("<@123> 살아있나?", False, MINE, [QUIET_CHANNEL]), "살아있나?")
        self.assertEqual(R.message_text("<@!123> !상태", False, MINE, [QUIET_CHANNEL]), "!상태")
        self.assertIsNone(R.message_text("<@123>", False, MINE, [QUIET_CHANNEL]))
        self.assertIsNone(R.message_text("<@123> 살아있나?", False, OTHERS, [QUIET_CHANNEL]))

    def test_empty_settings_keep_command_only(self):
        config.DISCORD_CHAT_GUILDS, config.DISCORD_CHAT_CHANNELS = [], []
        self.assertIsNone(R.message_text("살아있나?", False, MINE, [CHAT_CHANNEL]))
        self.assertEqual(R.message_text("!상태", False, MINE, [CHAT_CHANNEL]), "!상태")


class FileTexts(unittest.TestCase):
    def test_discord_channel_controls_are_routed_without_file_prefix(self):
        for text in ("!디스코드", "디스코드", "!디스코드 채널 지정", "디스코드 채널 삭제", "디스코드 채널 목록"):
            self.assertTrue(R.is_discord_command(text))
            self.assertEqual(R.file_text(text, False), text)
        for text in ("디스코드봇", "!디스코드파일", "!채널관리 등록", "파일 찾기 a"):
            self.assertFalse(R.is_discord_command(text))

    def test_plain_file_command_reaches_file_permission_check_in_any_channel(self):
        for text in ("파일", "파일 찾기 a", "파일 테스트에 메모 만들어줘", "파일 채널 지정"):
            self.assertEqual(R.file_text(text, False), text)
        for text in ("파일함", "파일test", "안녕", "내 파일 찾기"):
            self.assertIsNone(R.file_text(text, False))
        self.assertEqual(R.file_text("예", True), "예")


class ShowRows(Rules):
    def test_places(self):
        self.assertTrue(R.shows_rows(True))
        self.assertTrue(R.shows_rows(False, MINE, [QUIET_CHANNEL]))
        self.assertTrue(R.shows_rows(False, OTHERS, [CHAT_CHANNEL]))
        self.assertTrue(R.shows_rows(False, OTHERS, [99, CHAT_CHANNEL]))  # 스레드는 부모 채널을 따른다
        self.assertFalse(R.shows_rows(False, OTHERS, [QUIET_CHANNEL]))


class Split(unittest.TestCase):
    def test_short_text_stays_as_is(self):
        self.assertEqual(R.split_message("안녕"), ["안녕"])
        self.assertEqual(R.split_message("```\n표\n```"), ["```\n표\n```"])
        self.assertEqual(R.split_message(""), ["(빈 응답)"])

    def test_code_block_is_closed_and_reopened(self):
        text = "머리말\n```\n" + "\n".join("가" * 50 for _ in range(100)) + "\n```"
        chunks = R.split_message(text, limit=500)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(chunks[0].startswith("머리말\n```"))
        for chunk in chunks:
            self.assertEqual(chunk.count("```"), 2, chunk[:20])
            self.assertLessEqual(len(chunk), 508)
        self.assertEqual("\n".join(chunks).count("가"), 5000)

    def test_text_without_code_block_is_unchanged(self):
        chunks = R.split_message("\n".join("나" * 50 for _ in range(100)), limit=500)
        self.assertNotIn("```", "".join(chunks))


if __name__ == "__main__":
    unittest.main()
