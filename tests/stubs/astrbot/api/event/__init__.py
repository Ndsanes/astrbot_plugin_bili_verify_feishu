"""最小桩：astrbot.api.event，仅覆盖 bili_verify main.py 的导入面。"""


class AstrMessageEvent:
    def __init__(self, message_str="", group_id=None, sender_id="u1", self_id="10000"):
        self.message_str = message_str
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id


class PlatformAdapterType:
    AIOCQHTTP = "aiocqhttp"
    QQOFFICIAL = "qq_official"


class filter:
    class EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    PlatformAdapterType = PlatformAdapterType

    @staticmethod
    def command(*a, **k):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def event_message_type(*a, **k):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def llm_tool(name=None, **k):
        def deco(fn):
            return fn

        return deco
