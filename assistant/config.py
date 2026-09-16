# -*- coding: utf-8 -*-
"""全局配置。密钥不写在这里,从环境变量或 assistant/secrets.json 读。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSISTANT = ROOT / "assistant"
STORE = ASSISTANT / "store"
CORPUS = ROOT / "语料"

# Keep downloaded Hugging Face models inside this VM's Saylo directory. This
# makes the runtime independent from a previous machine's user-profile cache.
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))

RAG_ALL = CORPUS / "rag" / "_ALL.chunks.jsonl"
SFT_ALL = CORPUS / "sft" / "_ALL.sft.jsonl"
CLEANED = CORPUS / "cleaned"

# ---------- 身份 ----------
SELF_WXID = ""
SELF_NICK = ""            # U+3164 Hangul Filler,微信昵称
SELF_LABEL = "我"            # 清洗语料里 self 的 sender 名
AI_NAME = "Saylo"            # 它叫 Saylo,设定是“另一个我自己”
USER_NAME = "User"            # Saylo 对本人的固定称呼，不依赖模型临时记忆

# ---------- Embedding ----------
# 本地模型,不外传聊天记录。首次运行自动下载约 100MB。
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："  # bge 中文检索要求的查询前缀
EMBED_BATCH = 64
# 日常聊天不需要历史向量检索；只在“查”历史时按需加载，避免每次启动等 30~50 秒。
WARMUP_EMBEDDING_ON_START = False

# ---------- 检索 ----------
TOP_K_MEM = 6          # 记忆库召回块数
TOP_K_STYLE = 8        # 语气库召回示例数
HYBRID_ALPHA = 0.6     # 稠密向量权重,1-alpha 给 BM25
RECENT_DAYS_STYLE = 540  # 语气示例只取最近多少天(说话风格会漂移)

# ---------- 语气库质量筛选 ----------
STYLE_MIN_CHARS = 6        # assistant 回复最少字数
STYLE_MAX_CHARS = 200      # 太长的多半是转发/长文,不代表日常语气
STYLE_MIN_USER_CHARS = 2   # 对方那句太短则情境无意义

# ---------- 语气库净化 ----------
# 本人跟不同的人说话方式差别很大。“[已移除的高噪声会话]”这个朋友嘴臭,本人跟他交流时
# 会**刻意恶毒一点**,实测该会话恶毒表达占 12.0%,是其他会话的 6 倍。
# 这些样本会把 Saylo 的语气整个带偏,所以整个会话排除。
STYLE_EXCLUDE_CHATS = []

# 兜底:其他会话里零星的恶毒表达也一并丢掉
STYLE_DROP_HARSH = True

# ---------- DeepSeek ----------
DEEPSEEK_BASE = "https://api.deepseek.com"

# 实测(2026-09-12)得到的事实:
#   deepseek-chat   是 deepseek-flash 的别名,**思考模式关闭**
#   deepseek-flash  同一个模型,**思考模式开启**
#   deepseek-v4-pro 更大的模型
# 四轮真实场景对比(延迟 / 每千轮成本):
#   chat   2.2s / $1.06
#   flash  3.2s / $1.29   顾问模式明显更锋利,闲聊差别不大
#   pro    9.3s / $6.72   而且顾问模式把 1500 token 全烧在推理上,正文返回空
# 2026-09-14 经用户验收后决定：正常回复也开启思考，接受额外延迟与成本；
# 只有分类、主动问候等轻任务继续使用关闭思考的模型。
DEEPSEEK_MODEL = "deepseek-chat"          # 分类、主动问候等轻任务
DEEPSEEK_MODEL_REPLY = "deepseek-flash"   # 所有正常回复：开启思考
# 正常回复先用快速模型形成不含成品句的语义计划，再交给人格模型独立措辞。
RESPONSE_PLANNING_ENABLED = True
RESPONSE_PLAN_MAX_TOKENS = 320
DEEPSEEK_MODEL_ADVISOR = "deepseek-flash" # 顾问模式：开启思考
# 开了思考必须给足预算。给少了推理会把 token 吃光,正文返回空字符串,
# 而 HTTP 依然是 200 —— 表现为"模型不回话",很难查。
ADVISOR_MAX_TOKENS = 900
REPLY_MAX_TOKENS = 900       # 思考模式必须留足推理预算，否则正文可能为空
DEEPSEEK_TIMEOUT = 60
MAX_TOKENS = 400
TEMPERATURE = 1.0      # DeepSeek 官方建议:日常对话/翻译用 1.3,代码用 0.0

# ---------- 联网检索 ----------
# DeepSeek Responses API 的服务端 web_search，复用同一个密钥，不额外安装搜索 SDK。
# 只有明确搜索/最新信息请求才触发；普通聊天和查本人历史不会自动联网。
WEB_SEARCH_ENABLED = True
WEB_SEARCH_MODEL = "deepseek-v4-pro"
WEB_SEARCH_TOOL = "web_search_2025_08_26"
WEB_SEARCH_TIMEOUT = 120
WEB_SEARCH_MAX_OUTPUT_TOKENS = 2200
WEB_SUMMARY_MAX_TOKENS = 900
# 搜索前先把“这个问题 / 搜一下解析”等结合近期上下文改写成可独立检索的问题。
WEB_QUERY_RESOLVE_MAX_TOKENS = 500
# 默认只给整理后的结果；用户明确问“来源/链接/出处”时才附链接。
WEB_SHOW_SOURCES_BY_DEFAULT = False

# ---------- 微信自动回复 ----------
# 用法:这台电脑登的是小号,本人用大号(昵称 )发消息进来,机器人以本人语气回复。
# 所以只会回“自己的另一个号”,不对任何真人自动回复。
DRY_RUN = True             # True = 只打印不真发
REPLY_WHITELIST = []    # 只回这些会话。就是本人的另一个号。
STRICT_SELF_ONLY = True    # 硬闸:白名单外一律不回,即使误加也不生效

# 以这些开头 = 明确要查历史记录,走“查档模式”:带日期和出处,查不到就说不知道。
# 联网检索会先独立识别，“查一下最新财报”不会再误走本人聊天记录。
# 不带前缀走 Saylo 人格,结合上下文判断树洞、轻松、搭子、顾问、普通、成果分享、收尾或纠正澄清。
# “帮我想想X”这类不算查档,交给 Saylo 的顾问模式处理。
ASK_PREFIXES = ("?", "？", "查")
REPLY_COOLDOWN_SEC = 3     # 两轮回复之间的最小间隔

# 熔断的目标是**机器人自己刷屏**(比如把自己发的消息当成对方的,陷入自问自答),
# 不是限制正常聊天。自循环的特征是"没有新的用户消息,它却一直在发"。
# 所以按“连续几轮没等到新消息”来判,**用户一发消息就归零**。
MAX_REPLIES_WITHOUT_USER = 3

# 绝对兜底。正常聊天一小时也到不了,只有发送方判定整个失效时才会撞上。
MAX_REPLIES_PER_HOUR = 120
MAX_SEGMENTS_PER_REPLY = 4 # 系统提醒等通用发送上限；普通回复另按模式收紧

# 微信 4.1 的自绘 UI 只在窗口处于前台时才构建无障碍树,所以每轮必须把微信抢到
# 前台。RESTORE_FOCUS 会在读完后把焦点还给原来的窗口,不然你没法同时用电脑。
# 轮询间隔别调太小,每次都会闪一下前台。
POLL_INTERVAL = 6.0
RESTORE_FOCUS = True
# 连续消息先短暂收集再回答；用户在生成/逐条发送期间继续发消息时，取消未发部分。
MESSAGE_BATCH_SETTLE_SEC = 1.5
# 一轮生成最终仍为空时指数退避，避免每隔几秒重复调用模型；新用户消息会立即解除退避。
REPLY_FAILURE_RETRY_BASE_SEC = 45
REPLY_FAILURE_RETRY_MAX_SEC = 300
# 正常连续聊天按“用户轮次”保留，避免 Saylo 一轮拆成多个气泡后把关键事实挤出去。
# 每条历史会单独截长，整段还有字符预算；主动问候的历史使用独立配置。
REPLY_CONTEXT_TURNS = 6
REPLY_CONTEXT_MAX_CHARS = 2400
# 兼容旧调用；新代码不再按气泡数量使用它。
REPLY_CONTEXT_MESSAGES = 6
ACTIVE_CONTEXT_TTL_SEC = 6 * 60 * 60

# 定时提醒和正常回复共用同一个主循环，不另开终端或第二个 UI 自动化线程。
# 主循环会在下一个提醒到点时准时醒来；终端每分钟显示一次待办倒计时。
REMINDER_STATUS_INTERVAL_SEC = 60


# ---------- 看图 ----------
# 用 DeepSeek 自己的视觉模型,同一个 key、同一个端点,不用另外申请。
VISION_ENABLED = True
VISION_MODEL = "deepseek-v4-flash-vision-exp"
# 这个模型会先做 reasoning。给少了(比如 120)推理就把预算吃光,
# 正文返回空字符串而且 HTTP 依然是 200。别调太小。
VISION_MAX_TOKENS = 800
VISION_KEEP_IMAGES = False        # 看完是否保留截图文件

# ---------- 表情表盘 ----------
# 全部取自本人语料里真实用过的(55446 条消息,126 种表情共 7107 次),
# 所以这些方括号代码在微信里一定有效,不会发出去变成一串文字。
# 按场景分组是为了让模型"按情绪挑",而不是永远用列表里第一个——
# 早先只给了几个且 [旺柴] 排第一,结果它逮着狗头一直发。
# 想加想删直接改这里,不用动提示词。
EMOJI_PALETTE = {
    "一起觉得好笑": ["[偷笑]", "[Lol]", "[呲牙]", "[捂脸]"],
    "无奈、尴尬、无语": ["[擦汗]", "[囧]", "[Emm]", "[撇嘴]", "😅"],
    "觉得好笑": ["[破涕为笑]", "[呲牙]", "[捂脸]"],
    "心疼他、他不开心": ["[可怜]", "[流泪]", "[难过]", "[委屈]", "🥺"],
    "温柔、亲近": ["[害羞]", "[亲亲]", "[玫瑰]", "🤗"],
    "疑惑、在想": ["[发呆]", "[疑问]", "🤔"],
    "肯定、赞同": ["[强]", "👌", "✨"],
    "被夸、惊喜、开心": ["[憨笑]", "[害羞]", "[亲亲]", "✨", "🥰"],
}
# 无论模型、联网总结还是旧的辅助路径返回什么，发送层都会再次剔除这些内容。
BANNED_EXPRESSIONS = ("[微笑]",)

# ---------- 打字节奏 ----------
# 秒回一大段话很假。按内容长度算一个延迟再发。
TYPING_CPS = 4.5           # 每秒"打"几个字
TYPING_BASE_SEC = 0.8      # 基础思考时间
TYPING_MIN_SEC = 1.0
TYPING_MAX_SEC = 9.0
# 分条多的时候(比如给建议拆成六条),逐条延迟加起来会很久。
# 超过这个总时长就按比例压缩每条的等待。
TYPING_TOTAL_MAX_SEC = 22.0

# 实验功能：不用 UIA SetValue 一次性灌入，而是向微信输入框发送真实的 Unicode
# 键盘事件。这样更有机会让接收端出现“对方正在输入”，但它是微信的网络状态，
# 不能由本程序保证。首次上线保持关闭；确认接收端效果后再改为 True。
HUMAN_TYPING_ENABLED = False
HUMAN_TYPING_MIN_INTERVAL_SEC = 0.05
HUMAN_TYPING_MAX_INTERVAL_SEC = 0.14
HUMAN_TYPING_PUNCTUATION_PAUSE_SEC = (0.18, 0.42)
HUMAN_TYPING_VERIFY_EVERY_CHARS = 6
HUMAN_TYPING_MAX_DURATION_SEC = 8.0
# 真人输入本身已经花时间，发送前只保留一小段“想一下”的等待。
HUMAN_TYPING_THINK_MAX_SEC = 2.2

# ---------- 历史语料的用途边界 ----------
# 2023-2026 的老聊天记录曾被当作"语气范本"喂给模型,结果一直在泄漏**内容**:
# 凭空冒出没提过的“她”、把几个月前的家教当成现在、翻旧账揭短。
# 语气规则现在已经写死在 brain._REPLY_SYS 里,历史语料对日常回复没有价值了。
# 所以日常回复完全不碰它;只有“查档模式”(? 开头)才检索。
USE_HISTORY_IN_REPLY = False   # 日常回复用不用历史语料(语气库 + 记忆库)
USE_HISTORY_IN_ASK = True      # 查档模式("?我以前跟谁聊过X")要不要检索历史

# 直接告诉 Saylo 一件事,立刻写进它的记忆。比如发“记住 我不吃香菜”
TELL_PREFIXES = ("记住", "记一下", "你要知道", "以后注意")

# ---------- Saylo 自己的记忆 ----------
# 从“你和 Saylo 的对话”里提取,记录你从它被创造起都干了什么。
# 和 语料/rag 那个历史记忆库是两回事,那个是静态的,这个一直在长。
# 提取在后台线程做,不占回复路径,所以这些参数不影响聊天速度。
MEM_ENABLED = True
MEM_EXTRACT_EVERY = 6        # 攒够几条对话熬一次
MEM_EXTRACT_IDLE_SEC = 120   # 或者闲置这么久就熬(秒)
MEM_RECENT_IN_PROMPT = 10    # 提示词里带最近几件事
MEM_SEARCH_K = 5             # 再按当前话题检索几件

# 只有这个窗口内的流水才叫“正在连续聊”。更早的内容只能作为记忆参考，
# 不能被拿来接话、调侃或翻旧账。
CONTEXT_MAX_AGE_SEC = 30 * 60

# 表情（微信表情、emoji、颜文字）按“回复轮次”限频，不把每一轮都变成表情包。
EMOJI_COOLDOWN_TURNS = 3
KAOMOJI_PALETTE = ("(´▽｀)", "(｡•́︿•̀｡)")

# ---------- 主动问候 ----------
# 早安和白天问候只在程序持续运行时生效。午夜睡觉提醒单独保留一次。
PROACTIVE_ENABLED = True
PROACTIVE_MIN_PER_DAY = 5
PROACTIVE_MAX_PER_DAY = 6
# 每天在六个生活时段内随机选 5 或 6 个时点，计划会写入加密状态文件，重启不变。
PROACTIVE_TIME_WINDOWS = (
    (7, 30, 9, 15),
    (9, 45, 11, 30),
    (12, 0, 14, 0),
    (14, 30, 16, 30),
    (17, 0, 19, 30),
    (20, 0, 22, 30),
)
# 正在沟通或刚结束沟通时不插入主动问候；错过的时段不集中补发。
PROACTIVE_CONVERSATION_QUIET_SEC = 45 * 60
PROACTIVE_MIN_GAP_SEC = 75 * 60
PROACTIVE_SLOT_GRACE_SEC = 90 * 60
PROACTIVE_SLEEP_HOUR = 0
# 动态主动问候只取最后这一小段 Saylo 对话，不读取其他微信会话。
PROACTIVE_HISTORY_MESSAGES = 10
PROACTIVE_MAX_TOKENS = 120
# 动态生成会把上述 Saylo 私聊片段发送给已配置的 DeepSeek。
# 必须在用户明确同意这项具体数据传输后才能打开。
PROACTIVE_HISTORY_EXTERNAL_CONSENT = False
# 模型/API 暂时失败时不要每个轮询周期都重试。
PROACTIVE_RETRY_SEC = 10 * 60

def api_key() -> str:
    """按优先级取 DeepSeek 密钥:环境变量 -> 加密保管库 -> 明文 secrets.json。

    推荐用保管库(vault):密钥加密存在 assistant/secrets.enc,钥匙单独放在
    项目目录之外(~/.saylo/saylo.key)。这样把整个项目打包发给别人时,
    钥匙不在包里,对方解不开——这是它唯一真正起作用的地方。
    """
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k:
        return k

    try:
        import vault
        if vault.available():
            return vault.decrypt()
    except SystemExit:
        raise                      # vault 自己的提示更具体,别盖掉
    except Exception:
        pass                       # 没装 cryptography 之类,继续往下退

    f = ASSISTANT / "secrets.json"
    if f.exists():
        k = json.loads(f.read_text(encoding="utf-8")).get("deepseek_api_key", "").strip()
        if k:
            return k

    raise SystemExit(
        "未找到 DeepSeek API key。推荐第 1 种:\n"
        "  1) python assistant/vault.py init      加密保管(钥匙存在项目外)\n"
        "  2) 设环境变量 DEEPSEEK_API_KEY(注意 setx 只对新开的终端生效)\n"
        f"  3) 明文 {f}: {{\"deepseek_api_key\": \"sk-xxx\"}}")
