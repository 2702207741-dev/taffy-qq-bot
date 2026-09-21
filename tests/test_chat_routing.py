from __future__ import annotations

import unittest

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

from src.plugins.chat.routing import matches_command_text, persona_scope


class ChatRoutingTests(unittest.TestCase):
    def test_exact_commands_are_claimed(self) -> None:
        for command in (
            "大盘",
            "抄底",
            "菜单",
            "娱乐",
            "热门榜",
            "热度榜",
            "流动性榜",
            "我的预警",
            "定时状态",
            "K线",
            "k线",
            "塔菲记忆",
            "清空长期记忆",
            "清空对话",
            "塔菲风格",
            "撤销塔菲调整",
            "重置塔菲风格",
            "塔菲学习 状态",
        ):
            with self.subTest(command=command):
                self.assertTrue(matches_command_text(command))

    def test_short_structured_commands_are_claimed(self) -> None:
        for command in (
            "查询 红线",
            "K线 二西莫夫",
            "预警 红线 100",
            "十连开",
            "库存查询 76561198336029776",
            "记住 我喜欢短回答",
            "以后叫我 队长",
            "忘掉记忆 2",
            "塔菲学习 开",
        ):
            with self.subTest(command=command):
                self.assertTrue(matches_command_text(command))

    def test_conversational_or_overlong_text_falls_through(self) -> None:
        self.assertFalse(matches_command_text("查询一下这个皮肤为什么涨了"))
        self.assertFalse(matches_command_text("查询 " + "很长的皮肤名称" * 4))
        self.assertFalse(matches_command_text("我想查询红线"))
        self.assertTrue(matches_command_text("记住 " + "很长的偏好" * 20))

    def test_persona_scope_is_derived_from_private_or_group_event(self) -> None:
        private = PrivateMessageEvent(
            time=1,
            self_id=2,
            post_type="message",
            message_type="private",
            sub_type="friend",
            message_id=3,
            user_id=1001,
            message="hi",
            raw_message="hi",
            font=0,
            sender={"user_id": 1001, "nickname": "u", "sex": "unknown", "age": 0},
        )
        group = GroupMessageEvent(
            time=1,
            self_id=2,
            post_type="message",
            message_type="group",
            sub_type="normal",
            message_id=4,
            user_id=1001,
            group_id=2001,
            message="hi",
            raw_message="hi",
            font=0,
            sender={
                "user_id": 1001,
                "nickname": "u",
                "card": "",
                "sex": "unknown",
                "age": 0,
                "area": "",
                "level": "",
                "role": "member",
                "title": "",
            },
        )

        assert persona_scope(private).owner_user_id == 1001
        assert persona_scope(private).group_id == 0
        assert persona_scope(group).owner_user_id == 1001
        assert persona_scope(group).group_id == 2001


if __name__ == "__main__":
    unittest.main()
