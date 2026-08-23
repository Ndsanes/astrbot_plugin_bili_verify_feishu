"""最小桩 lark_oapi：仅为让 feishu_client 可离线导入（不发起任何真实调用）。"""


class LogLevel:
    WARNING = "warning"
    INFO = "info"
    ERROR = "error"


class _Builder:
    def app_id(self, v):
        return self

    def app_secret(self, v):
        return self

    def log_level(self, v):
        return self

    def build(self):
        return Client()


class Client:
    @staticmethod
    def builder():
        return _Builder()
