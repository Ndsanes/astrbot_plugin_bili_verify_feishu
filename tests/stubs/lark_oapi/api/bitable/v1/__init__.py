"""feishu_client 顶层导入的全部名字（新旧 SDK 两套请求体名都提供）。"""


def _dummy(name):
    return type(name, (), {})


CreateAppTableRecordRequest = _dummy("CreateAppTableRecordRequest")
CreateAppTableRecordResponse = _dummy("CreateAppTableRecordResponse")
CreateAppTableRecordRequestBody = _dummy("CreateAppTableRecordRequestBody")
SearchAppTableRecordRequest = _dummy("SearchAppTableRecordRequest")
SearchAppTableRecordRequestBody = _dummy("SearchAppTableRecordRequestBody")
SearchAppTableRecordResponse = _dummy("SearchAppTableRecordResponse")
UpdateAppTableRecordRequest = _dummy("UpdateAppTableRecordRequest")
UpdateAppTableRecordResponse = _dummy("UpdateAppTableRecordResponse")
AppTableRecord = _dummy("AppTableRecord")
