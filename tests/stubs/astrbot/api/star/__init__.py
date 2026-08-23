from astrbot.api.event import AstrMessageEvent  # noqa: F401


class Context:
    pass


class Star:
    def __init__(self, context, *a, **k):
        self.context = context


def register(*a, **k):
    def deco(cls):
        cls._register_args = a
        return cls

    return deco
