#!/usr/bin/env python3
"""
ContextLife 一周压力测试
========================
模拟一名博一新生（研究方向：World Model + 具身智能）的一周生活。
注入50+条消息，自动确认卡片，触发每日复盘、明日计划、Miku提醒。

使用方法：
  1. 启动服务器: python3 app.py
  2. 浏览器打开 http://localhost:5001
  3. 运行: python3 test_week.py
  4. 在浏览器中实时观察消息出现
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta

BASE_URL = "http://localhost:5001"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 一周日期: 周一 2026-02-22 到 周日 2026-02-28
WEEK_START = datetime(2026, 2, 22)
DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ============================================================
# HTTP 辅助函数（仅使用标准库，无需 requests）
# ============================================================

def http_get(path, timeout=30):
    """GET 请求，返回 (状态码, 解析后的JSON或None)"""
    url = BASE_URL + path
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


def http_post_form(path, fields, timeout=120):
    """POST 表单请求，返回 (状态码, 解析后的JSON或字符串)"""
    url = BASE_URL + path
    data = urllib.parse.urlencode(fields).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        return e.code, body
    except Exception as e:
        return 0, str(e)


def http_post_json(path, payload=None, timeout=120):
    """POST JSON请求，返回 (状态码, 解析后的JSON或字符串)"""
    url = BASE_URL + path
    data = json.dumps(payload or {}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as e:
        return 0, str(e)


# ============================================================
# 硬编码的50+条压力测试消息
# ============================================================

HARDCODED_MESSAGES = [
    # ===== 第1天 (周一 2026-02-22): 入学第一天 =====
    # 压测: 快速连续发送（30秒内3条），人物别名引入
    {"day": 1, "time": "08:30:00", "text": "今天是博士入学第一天！早上到了实验室，导师张伟教授跟我简单聊了聊研究方向，他建议我先从World Model的经典论文开始看起，特别是Ha和Schmidhuber 2018年那篇。"},
    {"day": 1, "time": "08:30:15", "text": "张老师还介绍了师兄李明给我认识，李明师兄在做具身智能方面的研究，用的是Isaac Gym仿真平台。"},
    {"day": 1, "time": "08:30:28", "text": "实验室的王芳师姐帮我配了工位的电脑，装了Ubuntu 22.04和CUDA 12.1，显卡是A100。"},
    {"day": 1, "time": "10:15:00", "text": "去图书馆借了几本参考书，遇到了同级博士生陈刚，他也是做AI方向的，我们约了周三一起讨论论文。"},
    {"day": 1, "time": "14:00:00", "text": "下午开始读World Models (Ha & Schmidhuber, 2018)，核心思想是用VAE+MDN-RNN做环境建模，agent在dream中学习。挺有意思的，但是VAE的重建效果可能不够好。"},
    {"day": 1, "time": "16:30:00", "text": "导师发消息说周五有组会，让我准备一个5分钟的自我介绍和研究兴趣报告。"},
    {"day": 1, "time": "20:00:00", "text": "晚上在宿舍继续看论文，顺便跑了一下World Models的开源代码，需要先配置gym环境。今天走了12000步，感觉还行。"},

    # ===== 第2天 (周二 2026-02-23): 深入阅读 =====
    # 压测: 纯任务完成，重复人物（张老师=导师=张伟教授）
    {"day": 2, "time": "09:00:00", "text": "读完了world model的论文，做了详细笔记。核心贡献是将model-based RL分解为V(vision)、M(memory)、C(controller)三个模块。"},
    {"day": 2, "time": "10:30:00", "text": "开始读DreamerV3，这篇是Hafner 2023年的工作，在World Models基础上大幅改进：用了symlog predictions和free bits等技巧，号称能跨不同domain泛化。"},
    {"day": 2, "time": "13:30:00", "text": "中午和李明师兄吃饭，他推荐我看一下RT-2 (Robotics Transformer 2)和Gato这两篇，说这是具身智能里VLM应用的代表作。还有一篇SayCan也很重要。"},
    {"day": 2, "time": "15:00:00", "text": "下午张老师来实验室转了一圈，问了我看论文的进展，让我整理一个reading list发给他，下周一交。优先级高，因为他要根据我的list调整研究计划。"},
    {"day": 2, "time": "17:00:00", "text": "配gym环境搞定了，主要是解决了一个mujoco的license问题。"},
    {"day": 2, "time": "21:00:00", "text": "晚上开始写组会presentation的slides，大概做了三分之一。今天只走了5000步，明天要多运动。"},

    # ===== 第3天 (周三 2026-02-24): 讨论+长消息+跨夜 =====
    # 压测: 超长消息（500+字符），接近午夜的消息，跨日消息
    {"day": 3, "time": "09:30:00", "text": "上午和陈刚在图书馆讨论了两个小时的论文。我们重点比较了World Models、DreamerV3、IRIS这三个方法的异同。World Models用VAE做视觉编码，DreamerV3改用了RSSM（Recurrent State Space Model），而IRIS用了discrete tokens（类似VQ-VAE）加Transformer。我觉得IRIS的方法更有前途，因为离散token表示天然适合和language model结合。陈刚觉得DreamerV3的鲁棒性更好，而且已经在很多benchmark上验证了。我们还讨论了一个共同感兴趣的问题：能不能把world model和LLM结合起来，让agent在自然语言引导下进行规划？这可能是一个很好的研究方向。具体来说，可以用LLM做high-level planning，用world model做low-level action prediction。陈刚建议我们每周三固定讨论一次。"},
    {"day": 3, "time": "11:30:00", "text": "刘思怡学姐（张伟教授的博士后）来实验室，她做的是robot manipulation方向。她给我推荐了一个很好的survey：A Survey on Large Language Model-based Autonomous Agents，说对我理解具身智能的全局图景很有帮助。"},
    {"day": 3, "time": "14:00:00", "text": "下午完善组会slides，加上了研究方向的初步想法：World Model + Language Grounding for Embodied Intelligence。"},
    {"day": 3, "time": "16:00:00", "text": "去操场跑了5公里，用时28分钟，感觉体力还不错。以后每周至少跑三次。"},
    {"day": 3, "time": "19:30:00", "text": "和本科同学赵丽视频聊天，她在阿里做NLP工程师，聊了聊工业界和学术界的差异。她说阿里那边也在搞具身智能，用的是通义千问做基座模型。"},
    {"day": 3, "time": "23:55:00", "text": "快到半夜了还在看IRIS的代码实现，发现它用了一个很巧妙的masking策略来处理partial observability。记录一下明天继续看。"},
    # 跨日消息：刚过午夜
    {"day": 4, "time": "00:10:00", "text": "忍不住又看了一会IRIS，终于理解了它的tokenizer是怎么训练的了。好了真的睡了。"},

    # ===== 第4天 (周四 2026-02-25): 代码+混合完成+特殊字符 =====
    # 压测: 代码片段，混合完成+新内容，特殊字符
    {"day": 4, "time": "09:00:00", "text": "今天开始尝试复现DreamerV3的核心模块。先搭了一个简单的RSSM：\n\n```python\nclass RSSM(nn.Module):\n    def __init__(self, stoch_size=32, deter_size=512, hidden_size=512):\n        super().__init__()\n        self.stoch_size = stoch_size\n        self.deter_size = deter_size\n        self.gru = nn.GRU(stoch_size + action_size, deter_size)\n        self.posterior = nn.Linear(deter_size + embed_size, 2 * stoch_size)\n        self.prior = nn.Linear(deter_size, 2 * stoch_size)\n```\n\n但跑的时候报了个错：\nRuntimeError: Expected hidden[0] size (1, 512), got (1, 256)\n\n需要debug一下hidden state维度。"},
    {"day": 4, "time": "11:00:00", "text": "搞定了DreamerV3的RSSM bug，然后又发现了一个新问题：symlog transform在梯度回传的时候numerical stability不好，loss会偶尔变成nan。需要加gradient clipping。"},
    {"day": 4, "time": "13:00:00", "text": "中午导师在微信群里发了一篇新的arXiv论文，是关于UniSim（Universal Simulator）的，用video generation model做world model。张伟教授说这个方向很有潜力，让我也加到reading list里。"},
    {"day": 4, "time": "14:30:00", "text": "组会slides搞定了，已经发给导师review了。顺便开始准备reading list文档。"},
    {"day": 4, "time": "16:00:00", "text": "和李明师兄结对调代码，他教我怎么用Isaac Gym设置一个简单的manipulation任务。需要注意：\n1. GPU pipeline比CPU快10倍+\n2. observation space要包含joint positions和object pose\n3. reward function设计要careful，sparse reward很难训\n\n今天又学到很多！"},
    {"day": 4, "time": "19:00:00", "text": "晚上整理reading list，目前有15篇论文了。按主题分了三类：World Models、Embodied Intelligence、LLM+Robot。"},
    {"day": 4, "time": "21:30:00", "text": "去跑了3公里，膝盖有点不舒服，可能是前天跑太快了。明天休息一天。"},

    # ===== 第5天 (周五 2026-02-26): 组会+多任务完成+紧急任务 =====
    # 压测: 快速发送，模糊任务完成匹配，大量活跃任务，紧急任务
    {"day": 5, "time": "08:45:00", "text": "组会前最后检查一遍slides，发现有个公式写错了，赶紧改。"},
    {"day": 5, "time": "08:45:20", "text": "好了改完了，DreamerV3的loss公式里beta_prior应该是0.5不是1.0。"},
    {"day": 5, "time": "08:45:45", "text": "导师说今天组会有个外校来的赵明远教授会旁听，他是做sim-to-real transfer的大牛。"},
    {"day": 5, "time": "10:00:00", "text": "组会顺利结束！赵明远教授对我的研究方向很感兴趣，提了几个很好的问题：1）world model的prediction horizon怎么确定 2）sim-to-real gap怎么处理 3）language grounding的评估metric怎么设计。我回答了前两个，第三个还需要再想想。张伟教授说我表现不错，让我下周把赵教授提的问题整理成一个研究问题文档。"},
    {"day": 5, "time": "11:30:00", "text": "赵明远教授临走前给了我他的邮箱，说如果有关于sim-to-real的问题可以随时邮件讨论。太nice了！"},
    {"day": 5, "time": "14:00:00", "text": "把reading list发给导师了，一共整理了18篇论文。顺便把research questions文档也开始写了。"},
    {"day": 5, "time": "15:30:00", "text": "导师紧急发消息：下周一有个基金申请的材料需要帮忙整理，今天或者明天要搞定！需要我整理近三年的相关工作综述部分。马上开始做。"},
    {"day": 5, "time": "17:00:00", "text": "开始搭建实验框架，选了PyTorch + Gymnasium (gym的新版本) + Weights & Biases做实验追踪。创了一个新repo叫world-model-embodied。"},
    {"day": 5, "time": "20:00:00", "text": "基金申请材料的相关工作部分写了一半，大概2000字。引用了12篇论文。明天继续写完。"},
    {"day": 5, "time": "22:00:00", "text": "今天信息量太大了，总结一下：组会很成功，认识了赵明远教授，但也多了好几个新任务。深呼吸，一件一件来。走了8000步，膝盖好了些。"},

    # ===== 第6天 (周六 2026-02-27): 集中攻关+批量完成 =====
    # 压测: 一次完成多个任务，时间冲突，大量任务
    {"day": 6, "time": "09:00:00", "text": "周六早上来实验室，先把基金申请材料的相关工作综述部分写完了，还把research questions文档也完善了，一共整理了五个核心研究问题。两个任务一起搞定的感觉真好！"},
    {"day": 6, "time": "10:30:00", "text": "开始认真梳理World Model + Embodied Intelligence的技术路线图。画了一个思维导图，主要分三层：Perception Layer (VAE/VQ-VAE)、World Model Layer (RSSM/Transformer)、Planning Layer (MPC/MCTS/LLM-guided)。"},
    {"day": 6, "time": "13:00:00", "text": "中午和王芳师姐、李明师兄、刘思怡学姐一起吃饭，讨论了实验室的GPU资源分配问题。目前实验室有8张A100，但经常不够用。师姐建议我先在小环境上验证想法，等有初步结果了再申请大规模GPU时间。"},
    {"day": 6, "time": "14:30:00", "text": "下午专注写代码，实现了一个简化版的World Model pipeline：\n1. 环境交互收集数据 -> replay buffer\n2. 训练 encoder + RSSM \n3. imagination rollouts + actor-critic\n\n目前在CartPole上能跑通，但reward曲线还不是很稳定。"},
    {"day": 6, "time": "17:00:00", "text": "尝试了一下把CartPole换成更复杂的Walker2d环境，直接崩了。应该是observation space维度变大后RSSM的capacity不够。需要调整网络结构。"},
    {"day": 6, "time": "19:00:00", "text": "周六晚上稍微放松一下，和陈刚去吃了火锅。聊了聊各自的进展，他在做的是multi-agent reinforcement learning，也遇到了收敛性问题。"},
    {"day": 6, "time": "21:30:00", "text": "回来后突然想到一个idea：能不能用CLIP做world model的视觉编码器，这样天然就有language alignment了。记下来下周详细想想。"},

    # ===== 第7天 (周日 2026-02-28): 总结反思+前瞻 =====
    {"day": 7, "time": "09:30:00", "text": "周日上午整理这一周的笔记和代码。创了一个Notion文档把所有论文笔记汇总了。"},
    {"day": 7, "time": "11:00:00", "text": "把这周的实验代码整理了一下，push到了GitHub上。README写了项目介绍和TODO list。"},
    {"day": 7, "time": "13:30:00", "text": "下午给赵明远教授发了一封邮件，总结了这周思考的三个核心研究问题，请教他的看法。顺便附了我的reading list。"},
    {"day": 7, "time": "15:00:00", "text": "跑了4公里，配速5分30秒/公里，膝盖完全好了。以后每次跑步都要先热身！"},
    {"day": 7, "time": "17:00:00", "text": "和爸妈视频通话，跟他们说了博士第一周的情况。妈妈说让我注意身体，别熬夜。确实这周有两天都搞到很晚。"},
    {"day": 7, "time": "19:00:00", "text": "今天回顾了一下这一周：读了十几篇论文，认识了五六个重要的人，创建了实验代码框架，参加了第一次组会。下周重点是：1）深入CLIP+World Model的idea 2）把Walker2d环境跑通 3）写research questions文档的最终版。"},
    {"day": 7, "time": "21:00:00", "text": "最后整理了一下明天的to-do：\n- 上午：去找导师讨论CLIP+World Model的想法\n- 下午：继续debug Walker2d实验\n- 晚上：准备周三和陈刚的讨论材料\n\n这周虽然累但收获很大，加油！"},
]


def clear_data():
    """清除所有数据文件，从头开始。"""
    print("\n  正在清除所有数据文件...")

    json_files = ["timeline.json", "cards.json", "persons.json",
                  "tasks.json", "reminders.json", "tomorrow_plan.json"]
    for f in json_files:
        path = os.path.join(DATA_DIR, f)
        if os.path.exists(path):
            os.remove(path)
            print(f"   已删除 {f}")

    uploads_dir = os.path.join(DATA_DIR, "uploads")
    if os.path.exists(uploads_dir):
        for f in os.listdir(uploads_dir):
            os.remove(os.path.join(uploads_dir, f))
        print("   已清空 uploads/")

    archive_dir = os.path.join(DATA_DIR, "archive")
    if os.path.exists(archive_dir):
        for f in os.listdir(archive_dir):
            os.remove(os.path.join(archive_dir, f))
        print("   已清空 archive/")

    print("   完成!\n")


def check_server():
    """检查服务器是否运行中。"""
    status, _ = http_get("/api/persons", timeout=5)
    if status == 200:
        print("  服务器运行中:", BASE_URL)
        return True
    print("  服务器未启动！请先运行: python3 app.py")
    return False


def send_message(text, sim_time, msg_idx, day_num):
    """发送消息到 /api/send 并处理响应。"""
    hhmm = sim_time.split(" ")[1][:5] if " " in sim_time else sim_time[:5]

    # 截断显示文本
    display = text[:70].replace("\n", " ")
    if len(text) > 70:
        display += "..."
    print(f"  [{hhmm}] 用户: {display}")

    status, resp = http_post_form("/api/send", {"text": text, "time": sim_time})

    if status != 200:
        err_text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)[:100]
        print(f"  ✗ 错误 {status}: {err_text}")
        return {"error": True, "status": status}

    if not isinstance(resp, dict):
        print(f"  ✗ 无效响应: {str(resp)[:100]}")
        return {"error": True, "msg": "无效响应"}

    if resp.get("error"):
        print(f"  ✗ 接口错误: {resp['error']}")
        return {"error": True, "msg": resp["error"]}

    response_type = resp.get("response_type", "card")
    result = {"error": False, "type": response_type}

    if response_type == "task_completion":
        completed = resp.get("completed_tasks", [])
        titles = [t["title"] if isinstance(t, dict) else t for t in completed]
        print(f"  ✓ 完成任务: {', '.join(titles)}")
        result["completed"] = titles

    elif response_type in ("card", "mixed"):
        summary = resp.get("summary", "")[:70]
        persons = [p.get("name", "") for p in resp.get("persons", [])]
        tasks = [t.get("title", "") for t in resp.get("tasks", [])]
        card_id = resp.get("id", resp.get("msg_id", ""))

        print(f"  [{hhmm}] Miru: {summary}")
        if persons:
            print(f"    人物: {', '.join(persons)}")
        if tasks:
            print(f"    任务: {', '.join(t[:30] for t in tasks)}")

        if response_type == "mixed":
            completed = resp.get("completed_tasks", [])
            titles = [t["title"] if isinstance(t, dict) else t for t in completed]
            print(f"    + 完成任务: {', '.join(titles)}")
            result["completed"] = titles

        # 自动确认卡片
        if card_id:
            cs, cr = http_post_json(f"/api/card/{card_id}/confirm", timeout=30)
            if cs == 200:
                print(f"    ✓ 卡片已确认")
                result["confirmed"] = True
            else:
                print(f"    ✗ 确认失败: {cs}")
                result["confirmed"] = False

        result["card_id"] = card_id
        result["persons"] = persons
        result["tasks"] = tasks

    return result


def run_daily_review(date_str, day_num):
    """触发指定日期的每日复盘。"""
    print(f"\n  📊 生成第{day_num}天每日复盘...")
    status, resp = http_post_json("/api/daily-review", {"date": date_str})

    if status != 200:
        print(f"    ✗ 错误 {status}")
        return None

    if not isinstance(resp, dict):
        print(f"    ✗ 无效响应")
        return None

    if resp.get("empty"):
        print(f"    (空) {resp.get('one_line', '无数据')}")
    else:
        one_line = resp.get("one_line", "")
        events = resp.get("events", {})
        total_events = sum(len(v) for v in events.values())
        tasks_new = len(resp.get("tasks", {}).get("new", []))
        tasks_open = len(resp.get("tasks", {}).get("open", []))
        persons_list = resp.get("persons", [])
        print(f"    一句话: {one_line}")
        print(f"    事件: {total_events}条, 新任务: {tasks_new}, 待办: {tasks_open}, 互动人物: {len(persons_list)}")
    return resp


def run_tomorrow_plan(date_str, day_num):
    """触发指定日期的明日计划。"""
    print(f"  📅 生成第{day_num}天明日计划...")
    status, resp = http_post_json("/api/tomorrow-plan", {"date": date_str})

    if status != 200:
        print(f"    ✗ 错误 {status}")
        return None

    if not isinstance(resp, dict):
        print(f"    ✗ 无效响应")
        return None

    if resp.get("empty"):
        print(f"    (空) {resp.get('one_line', '无数据')}")
    else:
        one_line = resp.get("one_line", "")
        top3 = resp.get("top3", [])
        schedule = resp.get("schedule", [])
        risks = resp.get("risks", [])
        print(f"    一句话: {one_line}")
        if top3:
            print(f"    重点任务: {', '.join(t.get('title', '')[:25] for t in top3)}")
        print(f"    时间块: {len(schedule)}个, 风险: {len(risks)}条")
    return resp


def test_reminders():
    """阶段2: 测试提醒系统，使用即将到来的时间块。"""
    print("\n" + "=" * 60)
    print("阶段2: 提醒系统测试")
    print("=" * 60)

    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 创建5个时间块: 前3个应该生成图片，后2个仅文字
    schedule = []
    for i in range(5):
        t = now + timedelta(minutes=1 + i)
        time_start = t.strftime("%H:%M")
        time_end = (t + timedelta(hours=1)).strftime("%H:%M")
        task_names = ["深度阅读论文", "代码复现实验", "整理研究笔记", "讨论研究方向", "写文档总结"]
        schedule.append({
            "time": f"{time_start}\u2013{time_end}",
            "task": f"测试任务{i+1}：{task_names[i]}",
            "type": "deep_work",
        })

    test_plan = {
        "date": today_str,
        "top3": [
            {"title": "测试任务1", "reason": "压力测试", "estimated_hours": 1, "priority": "high"},
        ],
        "schedule": schedule,
        "risks": [],
        "one_line": "测试提醒系统",
    }

    # 直接写入 tomorrow_plan.json
    plan_path = os.path.join(DATA_DIR, "tomorrow_plan.json")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(test_plan, f, ensure_ascii=False, indent=2)
    print(f"\n  已写入 tomorrow_plan.json: {len(schedule)}个时间块")
    print(f"  起始时间: {schedule[0]['time']}")
    print(f"  预期: 前3个生成图片，后2个仅文字 (每日3张限制)")

    # 轮询 reminders/check，最多6分钟
    print("\n  开始轮询 /api/reminders/check ...")
    triggered_count = 0
    image_count = 0
    text_only_count = 0
    max_polls = 36  # 36 * 10秒 = 6分钟
    poll_interval = 10

    for poll in range(max_polls):
        status, data = http_get("/api/reminders/check", timeout=30)
        if status == 200 and isinstance(data, dict):
            reminders = data.get("reminders", [])
            for rem in reminders:
                triggered_count += 1
                has_image = bool(rem.get("image_url"))
                if has_image:
                    image_count += 1
                else:
                    text_only_count += 1
                img_tag = "🖼️" if has_image else "📝"
                task_title = rem.get("task_title", "")
                rem_text = rem.get("text", "")[:50]
                print(f"    {img_tag} [{triggered_count}] {task_title}: {rem_text}...")

        if triggered_count >= 5:
            print(f"\n  ✅ 全部 {triggered_count} 个提醒已触发!")
            break

        time.sleep(poll_interval)
        if (poll + 1) % 3 == 0:
            elapsed = (poll + 1) * poll_interval
            print(f"    ... 已等待 {elapsed}秒, 已触发 {triggered_count}/5")

    print(f"\n  提醒结果: 已触发 {triggered_count}个, 含图片 {image_count}个, 仅文字 {text_only_count}个")
    if image_count <= 3:
        print("  ✅ 每日3张图片限制正常工作")
    else:
        print(f"  ❌ 图片限制失效: {image_count} > 3")

    return triggered_count, image_count, text_only_count


def final_verification():
    """阶段3: 最终数据完整性验证。"""
    print("\n" + "=" * 60)
    print("阶段3: 最终验证")
    print("=" * 60)

    issues = []

    # 人物库
    status, persons = http_get("/api/persons")
    if status == 200 and isinstance(persons, list):
        print(f"\n  👤 人物库: 共{len(persons)}人")
        for p in persons:
            tags = ", ".join(p.get("tags", []))
            mentions = len(p.get("mentions", []))
            print(f"    - {p.get('name', '?')} [{tags}] (提及{mentions}次)")

        # 检查人物去重
        zhang_names = [p for p in persons if "张伟" in p.get("name", "") or
                       "导师" in p.get("tags", []) or "张老师" in p.get("tags", [])]
        if len(zhang_names) > 1:
            issues.append(f"人物去重问题: 张伟教授出现了{len(zhang_names)}条记录")
            print(f"    ⚠️  张伟教授可能未正确去重: {[p['name'] for p in zhang_names]}")
        elif len(zhang_names) == 1:
            print(f"    ✅ 张伟教授去重正确")
    else:
        issues.append(f"人物库获取失败: {status}")

    # 任务列表
    status, tasks = http_get("/api/tasks")
    if status == 200 and isinstance(tasks, list):
        active = [t for t in tasks if t.get("status") != "completed"]
        completed = [t for t in tasks if t.get("status") == "completed"]
        print(f"\n  📋 任务: 共{len(tasks)}个, 进行中{len(active)}个, 已完成{len(completed)}个")
        for t in tasks[:15]:
            status_icon = "✅" if t.get("status") == "completed" else "🔲"
            priority = t.get("priority", "?")
            print(f"    {status_icon} [{priority}] {t.get('title', '')[:50]}")
        if len(tasks) > 15:
            print(f"    ... 还有{len(tasks) - 15}个")
    else:
        issues.append(f"任务列表获取失败: {status}")

    # 时间线
    status, timeline = http_get("/api/timeline")
    if status == 200 and isinstance(timeline, list):
        user_msgs = [e for e in timeline if e.get("type") != "reminder"]
        reminder_entries = [e for e in timeline if e.get("type") == "reminder"]
        confirmed = [e for e in user_msgs if e.get("status") == "confirmed"]
        print(f"\n  📜 时间线: 共{len(timeline)}条记录")
        print(f"    用户消息: {len(user_msgs)}条 (已确认{len(confirmed)}条)")
        print(f"    提醒: {len(reminder_entries)}条")

        pending = [e for e in user_msgs if e.get("status") == "pending"]
        if pending:
            issues.append(f"{len(pending)}条消息仍未确认")
            print(f"    ⚠️  {len(pending)}条消息仍未确认")
    else:
        issues.append(f"时间线获取失败: {status}")

    # 汇总
    print("\n" + "-" * 40)
    if issues:
        print(f"  ⚠️  发现{len(issues)}个潜在问题:")
        for issue in issues:
            print(f"    - {issue}")
    else:
        print("  ✅ 所有检查通过!")

    return issues


def main():
    print("=" * 60)
    print("  ContextLife 一周压力测试")
    print("  博一新生 · World Model + 具身智能")
    print(f"  共{len(HARDCODED_MESSAGES)}条消息，横跨7天")
    print("=" * 60)

    # 阶段0: 初始化
    if not check_server():
        sys.exit(1)

    clear_data()

    # 阶段1: 逐日消息注入
    print("=" * 60)
    print("阶段1: 消息注入（7天）")
    print("=" * 60)

    messages = HARDCODED_MESSAGES
    total_sent = 0
    total_cards = 0
    total_completions = 0
    total_errors = 0
    day_stats = {}

    for day_num in range(1, 8):
        date = WEEK_START + timedelta(days=day_num - 1)
        date_str = date.strftime("%Y-%m-%d")
        day_name = DAY_NAMES[day_num - 1]

        day_msgs = [m for m in messages if m["day"] == day_num]

        print(f"\n{'=' * 50}")
        print(f"第{day_num}天: {date_str} ({day_name}) — 共{len(day_msgs)}条消息")
        print(f"{'=' * 50}")

        day_card_count = 0
        day_completion_count = 0
        day_error_count = 0

        for idx, msg in enumerate(day_msgs):
            sim_time = f"{date_str} {msg['time']}"

            result = send_message(msg["text"], sim_time, idx, day_num)
            total_sent += 1

            if result.get("error"):
                day_error_count += 1
                total_errors += 1
            elif result.get("type") in ("card", "mixed"):
                day_card_count += 1
                total_cards += 1
                if result.get("completed"):
                    day_completion_count += len(result["completed"])
                    total_completions += len(result["completed"])
            elif result.get("type") == "task_completion":
                day_completion_count += len(result.get("completed", []))
                total_completions += len(result.get("completed", []))

            # 短暂等待，便于浏览器观察
            time.sleep(2)

        # 当天结束: 生成每日复盘 + 明日计划
        review = run_daily_review(date_str, day_num)
        time.sleep(3)
        plan = run_tomorrow_plan(date_str, day_num)
        time.sleep(3)

        day_stats[day_num] = {
            "msgs": len(day_msgs),
            "cards": day_card_count,
            "completions": day_completion_count,
            "errors": day_error_count,
        }

        print(f"\n  --- 第{day_num}天完成: {len(day_msgs)}条消息, "
              f"{day_card_count}张卡片, {day_completion_count}个任务完成, "
              f"{day_error_count}个错误 ---")

    # 阶段1 汇总
    print(f"\n{'=' * 60}")
    print("阶段1 汇总")
    print(f"{'=' * 60}")
    print(f"  总发送: {total_sent}条")
    print(f"  总卡片: {total_cards}张")
    print(f"  总完成: {total_completions}个任务")
    print(f"  总错误: {total_errors}个")
    for d, s in day_stats.items():
        print(f"  第{d}天: {s['msgs']}条消息, {s['cards']}张卡片, "
              f"{s['completions']}个完成, {s['errors']}个错误")

    # 阶段2: 提醒测试
    triggered, img_count, text_count = test_reminders()

    # 阶段3: 最终验证
    issues = final_verification()

    # 最终报告
    print(f"\n{'=' * 60}")
    print("最终报告")
    print(f"{'=' * 60}")
    print(f"  已处理消息: {total_sent}/{len(messages)}")
    print(f"  已创建卡片: {total_cards}张")
    print(f"  已完成任务: {total_completions}个")
    print(f"  接口错误: {total_errors}个")
    print(f"  已触发提醒: {triggered}个")
    print(f"  提醒图片: {img_count}张 (限制: 3张/天)")
    print(f"  发现问题: {len(issues)}个")
    if total_errors == 0 and len(issues) == 0:
        print("\n  🎉 压力测试全部通过!")
    else:
        print(f"\n  ⚠️  压力测试完成，共{total_errors + len(issues)}个问题")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
