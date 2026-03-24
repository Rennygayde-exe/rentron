from .responses import load_responses, match_response, RESPONSES, compile_triggers

__all__ = ["load_responses", "match_response", "RESPONSES", "compile_triggers"]
class DummyRole:
    def __init__(self, name):
        self.name = name

class DummyUser:
    def __init__(self):
        self.mention = "@Bot"
        self.id = 0
        self.roles = [DummyRole("Staff")]                                

class DummyMessage:
    async def edit(self, *args, **kwargs):
        pass

class DummyResponse:
    def is_done(self):
        return True

    async def defer(self, ephemeral=True):
        pass

    async def send(self, *args, **kwargs):
        return DummyMessage()                                              
    
    async def send_message(self, *args, **kwargs):
        pass

    async def edit(self, *args, **kwargs):
        pass
class DummyInteraction:
    def __init__(self, user=None, guild=None):
        self.user = user or DummyUser()
        self.guild = guild or type("Guild", (), {"text_channels": []})
        self.response = DummyResponse()
        self.followup = DummyResponse()
