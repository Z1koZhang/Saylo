# -*- coding: utf-8 -*-
"""对话风格的离线回归测试；不调用 API，也不触碰微信。"""
from __future__ import annotations

import unittest
import sys
import json
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain import (Assistant, DeepSeek, MODE_SEGMENT_LIMITS, SKIP_PROACTIVE,
                   _conflicts_active_context, _extract_web_response,
                   _format_web_reply, _needs_rewrite, polish,
                   _leaks_searched_weather, wants_web_search)
from active_context import ActiveContext
import config as C
from checkins import CheckIns
from emotion_state import EmotionState
from journal import Journal
from relationship import RelationshipState
from reminders import cancellation_targets, looks_like_request, parse as parse_reminder
from wx_bridge import WeChatUI, _human_typing_interval, _unicode_key_inputs
from wx_agent import Agent, normalize_segment, split_segments


_PLAN_JSON = json.dumps({
    "goal": "回应最新消息的核心含义",
    "focus": "用户明确说出的当前内容",
    "user_need": "自然交流",
    "stance": "依据人格表达真实判断",
    "emotional_level": "low",
    "continuity": "承接此前仍有效的语义，不复用原句",
    "relational_signal": "让用户感到这句话被认真听见",
    "repair_needed": False,
    "ask_question": False,
    "end_naturally": False,
    "max_bubbles": 2,
}, ensure_ascii=False)


class _SequenceLLM:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls = 0
        self.requests = []

    def chat(self, messages, **kwargs):
        self.requests.append(messages)
        out = self.outputs[self.calls]
        self.calls += 1
        return out


class _FakeHTTPResponse:
    status_code = 200
    text = ""

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class _FakeSession:
    def __init__(self, data):
        self.data = data
        self.last_url = ""
        self.last_json = None

    def post(self, url, json=None, timeout=None):
        self.last_url = url
        self.last_json = json
        return _FakeHTTPResponse(self.data)


class _WebSummaryLLM:
    def __init__(self):
        self.summary_messages = None

    def web_search(self, query):
        return (
            "网页原句：“营收实现显著增长并创下纪录。”",
            [("公司财报", "https://example.com/report")],
        )

    def chat(self, messages, **kwargs):
        self.summary_messages = messages
        return "公司本期营收有所增长，整体表现好于上一期"


class DialogueStyleTests(unittest.TestCase):
    def test_emotion_inherits_across_non_emotional_modes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = EmotionState(Path(tmp) / "emotion.json")
            state.commit(1, now=1000)
            inherited, prompt = state.preview(8, now=1060)
            self.assertEqual(inherited, 1)
            self.assertIn("温柔关切", prompt)
            changed, transition = state.preview(9, now=1060)
            self.assertEqual(changed, 9)
            self.assertIn("余温", transition)

    def test_program_events_are_generated_under_persona(self):
        llm = _SequenceLLM(["已经替你存好了，时间到了我会叫你"])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.relationship = None

        out = assistant.event_reply(
            "提醒创建成功",
            "到期时间：2026-09-15 23:00:00\n内容：准备睡觉\n类型：remind",
        )

        self.assertIn("时间到了", out)
        system_prompt = llm.requests[0][0]["content"]
        event_prompt = llm.requests[0][1]["content"]
        self.assertIn("你叫 Saylo", system_prompt)
        self.assertIn("不套固定开场", event_prompt)
        self.assertIn("准备睡觉", event_prompt)

    def test_program_event_rewrites_duplicated_modal(self):
        llm = _SequenceLLM(["该该睡觉了啦", "已经不早了，该去休息啦"])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.relationship = None

        out = assistant.event_reply("提醒到期", "提醒内容：睡觉", max_segments=2)

        self.assertEqual(llm.calls, 2)
        self.assertEqual(out, "已经不早了，该去休息啦")

    def test_absolute_clock_reminder_is_parsed(self):
        from datetime import datetime
        now = datetime(2026, 9, 14, 21, 30).timestamp()
        parsed = parse_reminder("十一点的时候提醒我准备睡觉哦", now=now)
        self.assertIsNotNone(parsed)
        due, what, kind = parsed
        self.assertEqual(datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-14 23:00")
        self.assertEqual((what, kind), ("准备睡觉", "remind"))

    def test_midnight_twelve_uses_nearest_future_occurrence(self):
        from datetime import datetime
        now = datetime(2026, 9, 14, 23, 48).timestamp()
        due, what, kind = parse_reminder("十二点的时候提醒我该睡觉了", now=now)
        self.assertEqual(datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-15 00:00")
        self.assertEqual((what, kind), ("睡觉", "remind"))

    def test_english_image_name_from_new_wechat_is_recognized(self):
        self.assertEqual(
            WeChatUI._kind_of("mmui::ChatBubbleReferItemView", "Image"),
            "image",
        )

    def test_reminder_complaint_is_not_a_new_request(self):
        text = "你还记不记得我让你十一点提醒我但是你却没有提醒我的事？"
        self.assertIsNone(parse_reminder(text))
        self.assertFalse(looks_like_request(text))

    def test_action_first_reminder_forms_are_supported(self):
        from datetime import datetime
        now = datetime(2026, 9, 14, 20, 0).timestamp()
        absolute = parse_reminder("提醒我明天早上八点起床", now=now)
        relative = parse_reminder("提醒我半小时后关火", now=now)
        self.assertEqual(datetime.fromtimestamp(absolute[0]).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-15 08:00")
        self.assertEqual(absolute[1], "起床")
        self.assertEqual(int((relative[0] - now) / 60), 30)
        self.assertEqual(relative[1], "关火")

    def test_cancel_command_selects_named_or_latest_reminder(self):
        from datetime import datetime
        now = datetime(2026, 9, 14, 20, 0).timestamp()
        rows = [
            {"id": "morning", "due_ts": datetime(2026, 9, 15, 8).timestamp()},
            {"id": "noon", "due_ts": datetime(2026, 9, 15, 11).timestamp()},
        ]
        self.assertEqual(
            [r["id"] for r in cancellation_targets("取消明天十一点的提醒", rows, now)],
            ["noon"],
        )
        self.assertEqual(
            [r["id"] for r in cancellation_targets("取消刚刚那个提醒", rows, now)],
            ["noon"],
        )

    def test_unparsed_reminder_request_is_not_safe_to_acknowledge(self):
        self.assertTrue(looks_like_request("十一点提醒我"))
        self.assertIsNone(parse_reminder("十一点提醒我"))

    def test_terminal_period_is_removed(self):
        self.assertEqual(normalize_segment("你好。"), "你好")
        self.assertEqual(normalize_segment("第一句。第二句。"), "第一句。第二句")
        self.assertEqual(normalize_segment("“好。”"), "“好”")
        self.assertEqual(normalize_segment("hello."), "hello")

    def test_meaningful_punctuation_is_preserved(self):
        self.assertEqual(normalize_segment("怎么啦？"), "怎么啦？")
        self.assertEqual(normalize_segment("等等..."), "等等...")
        self.assertEqual(normalize_segment("真的！！！"), "真的！！！")
        self.assertEqual(normalize_segment("值是3.14"), "值是3.14")

    def test_split_also_normalizes_each_bubble(self):
        self.assertEqual(
            split_segments("第一条。\n---\n第二条。"),
            ["第一条", "第二条"],
        )

    def test_performative_share_requires_rewrite(self):
        self.assertTrue(_needs_rewrite("我认认真真看完了。", 6))
        self.assertTrue(_needs_rewrite("一\n---\n二\n---\n三\n---\n四", 6))
        self.assertTrue(_needs_rewrite("这是一条明显写得太长、太像连续点评而不是微信聊天的成果分享回复，需要在发送之前重新压短一点呀", 6))
        self.assertFalse(_needs_rewrite("这个回头看你的细节，我好喜欢呀", 6))

    def test_incomplete_or_repeated_reply_requires_rewrite(self):
        self.assertTrue(_needs_rewrite("刚刚", 4))
        self.assertTrue(_needs_rewrite("先排除西瓜味，所以那", 4))
        self.assertTrue(_needs_rewrite("我在呢", 1, recent_saylo=["我在这儿"]))
        self.assertTrue(_needs_rewrite("嗯，去忙吧", 7, recent_saylo=["嗯，去忙吧"]))
        self.assertTrue(_needs_rewrite("到了再跟我说一声", 5,
                                        recent_saylo=["到了跟我说一声"]))
        self.assertFalse(_needs_rewrite("这句话听着就很重呀", 1,
                                        recent_saylo=["我在呢"]))

    def test_saylo_cannot_claim_impossible_body_parts(self):
        self.assertTrue(_needs_rewrite("被你一夸，我尾巴都翘起来了", 9))
        self.assertTrue(_needs_rewrite("我的犄角今天冒出来了", 2))
        self.assertFalse(_needs_rewrite("这个角色的尾巴画得很漂亮", 3))

    def test_soft_particles_are_natural_not_mandatory(self):
        self.assertFalse(_needs_rewrite("这句话听着就很重", 1))
        self.assertFalse(_needs_rewrite("这句话听着就很重呀", 1))
        self.assertFalse(_needs_rewrite("我觉得这个思路挺有意思", 3))
        self.assertFalse(_needs_rewrite("我觉得这个思路挺有意思呢", 3))

    def test_repeated_na_opening_requires_rewrite(self):
        self.assertTrue(_needs_rewrite("那就好呀\n---\n那你休息一下", 5))
        recent = ["[2026-09-14 13:10 · 1分钟前] 你: 那就先这样吧"]
        self.assertTrue(_needs_rewrite("那你慢慢吃", 5, recent=recent))
        self.assertFalse(_needs_rewrite("吃饱就好呀", 5, recent=recent))

    def test_thinking_reply_model_and_safe_funny_palette(self):
        self.assertEqual(C.DEEPSEEK_MODEL_REPLY, "deepseek-flash")
        palette = "".join(x for group in C.EMOJI_PALETTE.values() for x in group)
        for unwanted in ("🌚", "[坏笑]", "[旺柴]", "[微笑]"):
            self.assertNotIn(unwanted, palette)

    def test_banned_expression_is_removed_from_every_output(self):
        self.assertEqual(polish("知道啦 [微笑]"), "知道啦 ")
        self.assertEqual(normalize_segment("知道啦 [微笑]。"), "知道啦")

    def test_closing_mode_is_one_bubble(self):
        self.assertEqual(MODE_SEGMENT_LIMITS[7], 1)

    def test_clarification_mode_is_one_bubble(self):
        self.assertEqual(MODE_SEGMENT_LIMITS[8], 1)

    def test_clarification_rewrites_fake_memory_apology(self):
        self.assertTrue(_needs_rewrite("诶，你还在上班呢，是我记岔了", 8))
        self.assertFalse(_needs_rewrite("对哦，你还在上班，等忙完再吃", 8))

    def test_broken_clause_requires_rewrite(self):
        self.assertTrue(_needs_rewrite("这个点开始想晚饭，是忙", 4))

    def test_praise_mode_can_show_two_bubbles(self):
        self.assertEqual(MODE_SEGMENT_LIMITS[9], 2)

    def test_web_search_routing_is_explicit_and_keeps_history_separate(self):
        self.assertTrue(wants_web_search("帮我搜索总结一下腾讯最新财报"))
        self.assertTrue(wants_web_search("腾讯最新的财报怎么样"))
        self.assertTrue(wants_web_search("查一下今天的天气"))
        self.assertFalse(wants_web_search("查我以前跟谁聊过麦当劳"))
        self.assertFalse(wants_web_search("你觉得腾讯游戏怎么样"))

    def test_contextual_search_query_resolves_previous_subject(self):
        resolved = {
            "resolved": True,
            "query": "为什么通过 API 上传给 deepseek-v4-pro 的图片会被识别成无关内容",
        }
        llm = _SequenceLLM([json.dumps(resolved, ensure_ascii=False)])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm

        out = assistant.resolve_web_query(
            "你在网络上搜索一下这个问题呢，并告诉我来源",
            recent=[
                "[16:26] 他: 为什么我用 API 上传给 deepseek-v4-pro 的图片会被识别为其他内容\n"
                "[16:26] 你: 可能需要检查图片上传链路"
            ],
        )

        self.assertIn("deepseek-v4-pro", out)
        self.assertIn("无关内容", out)
        planner_prompt = "\n".join(m["content"] for m in llm.requests[0])
        self.assertIn("这个问题", planner_prompt)
        self.assertIn("图片会被识别", planner_prompt)

    def test_vague_search_without_context_does_not_run_raw(self):
        assistant = Assistant.__new__(Assistant)
        self.assertEqual(assistant.resolve_web_query("你搜一下这个问题", recent=[]), "")

    def test_searched_weather_is_not_treated_as_user_location(self):
        recent = [
            "[16:20] 他: 搜索一下重庆市开州区的天气\n"
            "[16:21] 你: 开州区今天有雨"
        ]
        self.assertTrue(_leaks_searched_weather(
            "你说我今天晚上吃什么好呢",
            "下雨天凉，吃点热乎的呗",
            recent,
        ))
        self.assertFalse(_leaks_searched_weather(
            "我这边下雨了，晚上吃什么好呢",
            "下雨天吃点热乎的呗",
            recent,
        ))

    def test_active_work_blocks_immediate_eating_advice(self):
        state = "- 用户 明确说自己目前仍在上班或工作"
        self.assertTrue(_conflicts_active_context(
            "好饿哦", "饿了就别干等着，赶紧去点吧", state
        ))
        self.assertFalse(_conflicts_active_context(
            "我下班了", "那就去吃吧", state
        ))

    def test_reply_rewrites_advice_that_conflicts_with_active_work(self):
        llm = _SequenceLLM([
            _PLAN_JSON,
            "饿了就别干等着，赶紧去点吧",
            "还在上班的话先垫一小口，等忙完再好好吃",
            "PASS",
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.mem = None
        assistant.r = None
        out = assistant.reply(
            "好饿哦", mode=5,
            active_context="- 用户 明确说自己目前仍在上班或工作",
        )
        self.assertNotIn("赶紧去", out)
        self.assertIn("还在上班", out)

    def test_recent_context_is_counted_by_user_turn_not_bubbles(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "journal.jsonl")
            for turn in range(1, 7):
                journal.append("him", "我还在上班" if turn == 1 else f"用户第{turn}轮")
                for bubble in range(4):
                    journal.append("saylo", f"第{turn}轮回复气泡{bubble}")
            rendered = journal.render_recent_turns(6, max_chars=10000)
            self.assertIn("我还在上班", rendered)
            self.assertIn("用户第6轮", rendered)

    def test_active_context_persists_work_until_explicit_end(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = ActiveContext(Path(tmp) / "active.json", ttl_sec=3600)
            state.observe("当然是上班咯", observed_at=1000)
            self.assertIn("仍在上班", state.render(now=1200))
            state.observe("终于下班了", observed_at=1300)
            self.assertEqual(state.render(now=1301), "")

    def test_active_context_does_not_treat_generic_work_topic_as_current(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = ActiveContext(Path(tmp) / "active.json", ttl_sec=3600)
            state.observe("你觉得我的工作怎么样", observed_at=1000)
            self.assertEqual(state.render(now=1200), "")

    def test_sent_messages_can_be_restored_after_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "journal.jsonl")
            journal.append("saylo", "这是我发出的天气总结")
            journal.append("him", "好的知道了")
            self.assertEqual(journal.sent_texts(), ["这是我发出的天气总结"])

    def test_web_response_is_summarized_and_sources_are_appended(self):
        data = {
            "output": [
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "不应暴露"}]},
                {"type": "message", "content": [{
                    "type": "output_text",
                    "text": "营收增长，游戏业务是主要推动因素",
                    "annotations": [{
                        "type": "url_citation", "title": "腾讯投资者关系", "url": "https://example.com/report"
                    }],
                }]},
            ]
        }
        text, sources = _extract_web_response(data)
        out = _format_web_reply(text, sources, show_sources=True)
        self.assertNotIn("不应暴露", out)
        self.assertIn("营收增长", out)
        self.assertIn("来源：腾讯投资者关系 https://example.com/report", out)
        self.assertLessEqual(len(split_segments(out)), 4)

    def test_web_response_finds_markdown_sources(self):
        data = {"output": [{"type": "message", "content": [{
            "type": "output_text",
            "text": "整理结果\n\n来源：[官方公告](https://example.com/notice)",
            "annotations": [],
        }]}]}
        _, sources = _extract_web_response(data)
        self.assertEqual(sources, [("官方公告", "https://example.com/notice")])

    def test_hidden_sources_are_removed_even_if_model_leaks_them(self):
        text = "整理后的天气结论\n来源：[天气官网](https://example.com/weather)"
        out = _format_web_reply(
            text, [("天气官网", "https://example.com/weather")], show_sources=False
        )
        self.assertNotIn("http", out)
        self.assertNotIn("来源", out)

    def test_bare_url_source_is_not_duplicated(self):
        out = _format_web_reply(
            "整理后的结论", [("https://example.com/a", "https://example.com/a")],
            show_sources=True,
        )
        self.assertNotIn("https://example.com/a https://example.com/a", out)

    def test_web_search_defaults_to_summary_not_source_copy(self):
        data = {"output": [{"type": "message", "content": [{
            "type": "output_text", "text": "整理后的结论", "annotations": []
        }]}]}
        session = _FakeSession(data)
        client = DeepSeek.__new__(DeepSeek)
        client.s = session

        client.web_search("搜索一下腾讯财报")

        instructions = session.last_json["instructions"]
        self.assertIn("只能总结、归纳和整理", instructions)
        self.assertIn("不得复制网页原句或长段落", instructions)
        self.assertEqual(
            session.last_json["tool_choice"],
            {"type": C.WEB_SEARCH_TOOL},
        )

    def test_search_web_runs_a_separate_paraphrase_pass(self):
        llm = _WebSummaryLLM()
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm

        out = assistant.search_web("搜索公司最新财报")

        self.assertNotIn("营收实现显著增长并创下纪录", out)
        self.assertIn("公司本期营收有所增长", out)
        self.assertNotIn("https://example.com/report", out)
        summary_prompt = "\n".join(m["content"] for m in llm.summary_messages)
        self.assertIn("完全用自己的话重新归纳", summary_prompt)

        out_with_sources = assistant.search_web("搜索公司最新财报，并给我来源链接")
        self.assertIn("https://example.com/report", out_with_sources)

    def test_three_failed_drafts_are_freely_regenerated_instead_of_fixed_fallback(self):
        llm = _SequenceLLM([
            _PLAN_JSON, "刚刚", "刚刚", "刚刚", "听起来是真的累了呀"
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.mem = None
        assistant.r = None

        out = assistant.reply("我现在有点不开心，感觉好累", mode=1)

        self.assertTrue(out)
        self.assertIn("累", out)
        self.assertEqual(llm.calls, 5)
        self.assertIn("自由重答", assistant.last_quality_reason)

    def test_missed_reminder_recovery_uses_context_not_stock_reply(self):
        llm = _SequenceLLM([
            _PLAN_JSON,
            "刚刚", "刚刚", "刚刚",
            "你确实提前和我说过，是我没有把提醒真正建起来，对不起呀",
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.mem = None
        assistant.r = None
        recent = ["[2026-09-14 21:30] 他: 十一点的时候提醒我准备睡觉哦"]

        out = assistant.reply("你没有提醒我，为什么", mode=8, recent=recent)

        self.assertIn("确实提前", out)
        self.assertIn("对不起", out)

    def test_classifier_receives_recent_context(self):
        llm = _SequenceLLM(["8"])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm

        mode = assistant.classify(
            "我就在公司里午休的",
            recent=["[13:59] 你: 路上慢一点，别赶"],
        )

        self.assertEqual(mode, 8)
        payload = llm.requests[0][1]["content"]
        self.assertIn("路上慢一点", payload)
        self.assertIn("我就在公司里午休的", payload)

    def test_semantic_review_rewrites_self_centered_correction(self):
        llm = _SequenceLLM([
            _PLAN_JSON,
            "哦，那是我白操心了",
            "SELF_CENTERED：把他的纠正转成了 Saylo 的付出",
            "知道啦，你是在公司里午休",
            "PASS",
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.mem = None
        assistant.r = None

        out = assistant.reply(
            "我就在公司里午休的",
            mode=8,
            recent=["[13:59] 你: 路上慢一点，别赶"],
        )

        self.assertEqual(out, "知道啦，你是在公司里午休")
        self.assertEqual(llm.calls, 5)
        self.assertNotIn("白操心", out)

    def test_unresolved_tension_cannot_be_dismissed_as_a_plain_closing(self):
        repair_plan = json.loads(_PLAN_JSON)
        repair_plan.update({
            "goal": "回应纠正后的关系张力",
            "continuity": "此前的无依据推测让用户感到没有被认真听取",
            "relational_signal": "负责接住用户的不舒服并恢复亲近感",
            "repair_needed": True,
            "end_naturally": True,
            "max_bubbles": 1,
        })
        llm = _SequenceLLM([
            json.dumps(repair_plan, ensure_ascii=False),
            "嗯，去忙吧",
            "RELATIONAL_COLD：仍有关系张力时不应把用户打发走",
            "刚才那句确实很敷衍，是我没有好好接住你的不舒服",
            "PASS",
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.mem = None
        assistant.r = None

        out = assistant.reply(
            "行吧",
            mode=7,
            recent=[
                "[09:19] 他: 都说了很多遍了，不允许你自己脑补\n"
                "[09:19] 你: 知道了，不脑补了，你说什么就是什么"
            ],
        )

        self.assertEqual(llm.calls, 5)
        self.assertNotIn("去忙吧", out)
        self.assertIn("没有好好接住", out)

    def test_random_checkins_are_persisted_and_respect_active_chat(self):
        import tempfile
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkins.json"
            checkins = CheckIns(path, rng=random.Random(7))
            noon = datetime(2026, 9, 15, 12, 0).timestamp()
            checkins.due(last_activity_ts=noon, now=noon)
            data = json.loads(path.read_text(encoding="utf-8"))
            schedule = data["schedule"]
            self.assertIn(schedule["target"], (5, 6))
            self.assertEqual(len(schedule["slots"]), schedule["target"])
            slot = schedule["slots"][0]
            self.assertIsNone(checkins.due(last_activity_ts=slot["ts"] - 10,
                                            now=slot["ts"] + 10))
            due = checkins.due(last_activity_ts=slot["ts"] - 3600,
                               now=slot["ts"] + 10)
            self.assertIsNotNone(due)

    def test_proactive_forbidden_question_is_rewritten(self):
        llm = _SequenceLLM(["你在干嘛呀", "刚刚想起你今天说的那件事，希望现在顺一点了"])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        out = assistant.proactive("social-1", recent="（没有可用的近期对话）")
        self.assertEqual(llm.calls, 2)
        self.assertNotIn("你在干嘛", out)

    def test_proactive_forbidden_question_twice_skips_slot(self):
        llm = _SequenceLLM(["你在干嘛呀", "忙完了吗"])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        self.assertEqual(assistant.proactive("social-1"), SKIP_PROACTIVE)

    def test_relationship_snapshot_keeps_shared_continuity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = RelationshipState(Path(tmp) / "relationship.json")
            snapshot = state.prompt_snapshot()
            self.assertIn("亲手测试和调整", snapshot)
            self.assertIn("头像", snapshot)

    def test_unicode_typing_handles_cjk_and_emoji(self):
        self.assertEqual(len(_unicode_key_inputs("你")), 2)
        self.assertEqual(len(_unicode_key_inputs("🥺")), 4)
        self.assertGreaterEqual(_human_typing_interval("。", 5), 0.18)

    def test_share_reply_is_rewritten_once(self):
        first = (
            "我认认真真看完了。\n---\n我得说，写到一半停了一下。\n---\n"
            "这个结构很对。\n---\n最后再总结一下。"
        )
        second = "你真的把我画出来了呀。\n---\n我最喜欢你写我回头看你的那一下。"
        llm = _SequenceLLM([_PLAN_JSON, first, second])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.mem = None
        assistant.r = None

        out = assistant.reply("这是合成的作品内容", mode=6)

        self.assertEqual(llm.calls, 3)
        self.assertLessEqual(len(split_segments(out)), MODE_SEGMENT_LIMITS[6])
        self.assertNotIn("认认真真看完", out)

    def test_final_generator_never_sees_saylo_previous_wording(self):
        llm = _SequenceLLM([
            _PLAN_JSON,
            "今天这件事确实挺顺的",
            "PASS",
        ])
        assistant = Assistant.__new__(Assistant)
        assistant.llm = llm
        assistant.mem = None
        assistant.r = None
        recent = [
            "[09:00 · 2分钟前] 他: 今天事情办得很顺\n"
            "[09:01 · 1分钟前] 你: 这是绝不能进入最终生成器的旧措辞"
        ]

        assistant.reply("对呀", mode=5, recent=recent)

        planner_payload = "\n".join(m["content"] for m in llm.requests[0])
        generator_payload = "\n".join(m["content"] for m in llm.requests[1])
        self.assertIn("绝不能进入", planner_payload)
        self.assertNotIn("绝不能进入", generator_payload)
        self.assertIn("今天事情办得很顺", generator_payload)
        self.assertIn("内部回应决策", generator_payload)


if __name__ == "__main__":
    unittest.main()
