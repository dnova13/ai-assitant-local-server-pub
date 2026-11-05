"""비서 앱의 질문을 디스코드 버튼으로 표시한다. 판정·실행은 앱이 담당한다."""
import asyncio
import time
import discord


class FilePromptView(discord.ui.View):
    def __init__(self, prompt, author_id, answer):
        super().__init__(timeout=max(.01, prompt['expiresAt'] / 1000 - time.time()))
        self.prompt, self.author_id, self.answer = prompt, author_id, answer
        self.message, self.used = None, False
        self.lock = asyncio.Lock()
        if prompt['kind'] == 'confirm':
            yes = ('예', '예', discord.ButtonStyle.danger if prompt.get('destructive') else discord.ButtonStyle.primary)
            no = ('아니오', '아니오', discord.ButtonStyle.secondary)
            buttons = [no, yes] if prompt.get('destructive') else [yes, no]
        else:
            buttons = [(str(i + 1), str(i + 1), discord.ButtonStyle.primary) for i in range(len(prompt['options']))]
            buttons.append(('취소', '취소', discord.ButtonStyle.secondary))
        for i, (label, value, style) in enumerate(buttons):
            button = discord.ui.Button(label=label, style=style, custom_id=f"file:{prompt['id']}:{i}", row=i // 5)
            async def clicked(interaction, value=value):
                await self.clicked(interaction, value)
            button.callback = clicked
            self.add_item(button)

    async def clicked(self, interaction, value):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message('요청한 사람만 선택할 수 있습니다.', ephemeral=True)
            return
        async with self.lock:
            if self.used or time.time() * 1000 >= self.prompt['expiresAt']:
                await interaction.response.send_message('이미 끝난 질문입니다.', ephemeral=True)
                return
            self.used = True
        await interaction.response.defer()
        await self.close()
        await self.answer(value, self.prompt['id'])

    async def close(self):
        self.used = True
        self.stop()
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        await self.close()
