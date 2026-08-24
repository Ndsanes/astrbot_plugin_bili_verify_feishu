"""platform_port.py — JoinRequest DTO

归一化入群申请数据结构，供 AdmissionService.admit_request 与 main.py 的
两条入群链路（aiocqhttp 推送 / qq_official 轮询）共用。

历史上本文件还承载 PlatformPort 协议与 QqOfficialAdapter / OneBotAdapter
适配器，但生产链路从未接线（平台调用直接走各自事件/轮询路径），已整体移除。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class JoinRequest:
    """归一化入群申请。

    Attributes:
        group_openid: 群标识（qq_official group_openid / aiocqhttp group_id，统一 str）
        join_request_id: QQ 官方返回的 join_request_id；OneBot 侧复用 flag/request_id
        member_openid: 申请人 openid / user_id（统一为 str，数字 QQ 也转 str）
        username: 申请人昵称（可能为空，resolve_nickname 可二次解析）
        comment: 校验信息（QQ 官方 verify_info.verify_message 与 review_qa_list
            拼接；OneBot 侧 comment/message）
        raw: 原始条目，便于透传 sub_type / verify_info 等
    """

    group_openid: str = ""
    join_request_id: str = ""
    member_openid: str = ""
    username: str = ""
    comment: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


__all__ = ["JoinRequest"]
