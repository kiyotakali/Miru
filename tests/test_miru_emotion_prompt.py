"""Test Miru emotion evaluation prompt and call_miru_emotion_eval (V4 — 2026-05-09).

V4 design: closeness/trust meters were removed. The eval LLM now only emits
mood/valence/arousal/reason. The "你和对方的关系" context section is gone, and
soul.md's `# Relationship Stages` block is the sole driver of tonal cues
(injected upstream by prompt._build_chat_system_prompt, not here).

call_miru_emotion_eval signature: trigger_kind / trigger_text / today_chat /
seconds_since_user_msg / seconds_since_miru_proactive / user_emotion_arc /
current_state / days_together / human_block / persona_block /
optional trigger_image_path for multimodal chat events.
"""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_prompt_contains_soul_and_blocks():
    """System prompt embeds soul.md + human + persona core_memory blocks."""
    print("=" * 60)
    print("TEST: System prompt — soul + human + persona injection")
    print("=" * 60)

    from memory_prompts import _build_miru_emotion_prompt

    passed = 0
    total = 0

    p_empty = _build_miru_emotion_prompt()
    from character import get_config
    cfg = get_config()

    total += 1
    if cfg.name in p_empty:
        print(f"  [PASS] character name '{cfg.name}' present")
        passed += 1
    else:
        print(f"  [FAIL] character name not in prompt")

    total += 1
    if "valence" in p_empty and "arousal" in p_empty:
        print(f"  [PASS] valence/arousal guidance present")
        passed += 1
    else:
        print(f"  [FAIL] valence/arousal guidance missing")

    # closeness/trust guidance must be GONE — it was removed in 2026-05-09 along
    # with the meter system. If it comes back the test catches a regression.
    total += 1
    if "closeness_delta" not in p_empty and "trust_delta" not in p_empty:
        print(f"  [PASS] closeness/trust delta guidance ABSENT (removed)")
        passed += 1
    else:
        print(f"  [FAIL] delta guidance leaked back into prompt")

    # The prompt now asserts "you're already close" so Miru's tone defaults warm.
    total += 1
    if "已经很亲近" in p_empty:
        print(f"  [PASS] '你们已经很亲近' tone anchor present")
        passed += 1
    else:
        print(f"  [FAIL] closeness tone anchor missing")

    total += 1
    if "时间感是真实的" in p_empty:
        print(f"  [PASS] 时间感 section present (replaces absence preset)")
        passed += 1
    else:
        print(f"  [FAIL] 时间感 section missing")

    total += 1
    if "看自己说过的话有没有被回应" in p_empty:
        print(f"  [PASS] self-response-check guidance present")
        passed += 1
    else:
        print(f"  [FAIL] self-response-check guidance missing")

    total += 1
    if "对方情绪也会感染你" in p_empty:
        print(f"  [PASS] emotional-contagion guidance present")
        passed += 1
    else:
        print(f"  [FAIL] emotional-contagion guidance missing")

    p_full = _build_miru_emotion_prompt(
        human_block="用户是研究生，方向是世界模型。",
        persona_block="认识 3 个月，最近开始能听懂他研究的方向。",
    )
    total += 1
    if "世界模型" in p_full and "研究的方向" in p_full:
        print(f"  [PASS] human + persona blocks injected when provided")
        passed += 1
    else:
        print(f"  [FAIL] human/persona blocks NOT injected")

    print(f"\n  SystemPrompt: {passed}/{total} passed")
    return passed, total


def test_call_miru_emotion_eval_valid():
    """Valid LLM response → result dict with normalized numeric types."""
    print("\n" + "=" * 60)
    print("TEST: call_miru_emotion_eval — valid response")
    print("=" * 60)

    from memory_prompts import call_miru_emotion_eval

    passed = 0
    total = 0

    mock_llm_result = {
        "mood": "有点心疼",
        "valence": -0.2,
        "arousal": 0.4,
        "reason": "对方说期末压力大",
    }

    with patch("prompt._call_retrieval_llm", return_value=mock_llm_result) as mock_call:
        result = call_miru_emotion_eval(
            trigger_kind="chat",
            trigger_text="快到期末了好烦啊",
            today_chat="  [09:30] 你：早\n  [20:00] 你：快到期末了好烦啊",
            seconds_since_user_msg=10,
            seconds_since_miru_proactive=None,
            user_emotion_arc="20:00 stressed (期末压力)",
            current_state={"mood": "平静", "valence": 0.0, "arousal": 0.2, "reason": ""},
            days_together=14,
            human_block="用户是研究生",
            persona_block="认识 2 周",
        )

        total += 1
        if result is not None:
            print(f"  [PASS] got result")
            passed += 1
        else:
            print(f"  [FAIL] result is None")

        total += 1
        if result and result["mood"] == "有点心疼" and result["valence"] == -0.2:
            print(f"  [PASS] mood + valence preserved")
            passed += 1
        else:
            print(f"  [FAIL] fields wrong: {result}")

        # Verify LLM was called and the user_text contained key context
        total += 1
        if mock_call.called:
            user_text = mock_call.call_args[0][1]
            checks = {
                "trigger_text": "快到期末了好烦啊" in user_text,
                "today_chat": "[20:00] 你" in user_text,
                "current_state": "平静" in user_text,
                "user_arc": "stressed" in user_text or "期末压力" in user_text,
                "days_together": "第 15 天" in user_text or "第 14 天" in user_text,
            }
            if all(checks.values()):
                print(f"  [PASS] all expected context sections in user_text")
                passed += 1
            else:
                missing = [k for k, v in checks.items() if not v]
                print(f"  [FAIL] missing sections: {missing}")
        else:
            print(f"  [FAIL] LLM not called")

        # closeness numeric must NOT appear in user_text — that section was deleted.
        total += 1
        if mock_call.called:
            user_text = mock_call.call_args[0][1]
            if "亲密度" not in user_text and "信任度" not in user_text:
                print(f"  [PASS] closeness/trust labels absent from user_text")
                passed += 1
            else:
                print(f"  [FAIL] closeness/trust label leaked into user_text")

    print(f"\n  ValidCall: {passed}/{total} passed")
    return passed, total


def test_call_miru_emotion_eval_invalid():
    """Invalid LLM responses → None gracefully."""
    print("\n" + "=" * 60)
    print("TEST: call_miru_emotion_eval — invalid responses")
    print("=" * 60)

    from memory_prompts import call_miru_emotion_eval

    passed = 0
    total = 0
    common_kwargs = dict(
        trigger_kind="chat",
        trigger_text="hi",
        today_chat="",
        current_state={"mood": "neutral", "valence": 0, "arousal": 0.2},
        days_together=0,
    )

    # Non-dict
    total += 1
    with patch("prompt._call_retrieval_llm", return_value="not a dict"):
        if call_miru_emotion_eval(**common_kwargs) is None:
            print(f"  [PASS] non-dict → None")
            passed += 1
        else:
            print(f"  [FAIL] non-dict should be None")

    # Missing fields
    total += 1
    with patch("prompt._call_retrieval_llm", return_value={"arousal": 0.3}):
        if call_miru_emotion_eval(**common_kwargs) is None:
            print(f"  [PASS] missing mood/valence → None")
            passed += 1
        else:
            print(f"  [FAIL] missing fields should be None")

    # LLM exception
    total += 1
    with patch("prompt._call_retrieval_llm", side_effect=Exception("API down")):
        if call_miru_emotion_eval(**common_kwargs) is None:
            print(f"  [PASS] LLM exception → None")
            passed += 1
        else:
            print(f"  [FAIL] exception should be None")

    # Non-numeric valence
    total += 1
    with patch("prompt._call_retrieval_llm",
               return_value={"mood": "happy", "valence": "abc"}):
        if call_miru_emotion_eval(**common_kwargs) is None:
            print(f"  [PASS] non-numeric valence → None")
            passed += 1
        else:
            print(f"  [FAIL] non-numeric should be None")

    # String numbers should be coerced (mood/valence/arousal only — no deltas any more)
    total += 1
    with patch("prompt._call_retrieval_llm", return_value={
        "mood": "开心", "valence": "0.5", "arousal": "0.4", "reason": "t",
    }):
        result = call_miru_emotion_eval(**common_kwargs)
        if result and result["valence"] == 0.5 and result["arousal"] == 0.4:
            print(f"  [PASS] string numbers coerced for valence/arousal")
            passed += 1
        else:
            print(f"  [FAIL] string number coercion failed: {result}")

    print(f"\n  Invalid: {passed}/{total} passed")
    return passed, total


def test_screenshot_path_text_only():
    """Screenshot trigger always uses text path. After 2026-05-10 vision
    redesign the chat path is also text-only (image_desc spliced into
    trigger_text upstream), so this test simply verifies *no* multimodal
    call ever happens regardless of trigger_kind.
    """
    print("\n" + "=" * 60)
    print("TEST: screenshot trigger — text-only path")
    print("=" * 60)

    from memory_prompts import call_miru_emotion_eval
    passed = 0
    total = 0

    text_calls = []

    def mock_text(system, user_text, **kwargs):
        text_calls.append(user_text)
        return {"mood": "平静", "valence": 0.05, "arousal": 0.2, "reason": ""}

    with patch("prompt._call_retrieval_llm", side_effect=mock_text), \
         patch("prompt._call_llm_multimodal_json") as mock_mm:
        call_miru_emotion_eval(
            trigger_kind="screenshot",
            trigger_text="用户在 VS Code 里看代码",
            today_chat="",
            current_state={"mood": "neutral", "valence": 0, "arousal": 0.2},
            days_together=0,
        )

        total += 1
        if text_calls and not mock_mm.called:
            print(f"  [PASS] screenshot path used text LLM (no multimodal)")
            passed += 1
        else:
            print(f"  [FAIL] expected text-only, got text={bool(text_calls)} mm_called={mock_mm.called}")

        total += 1
        if text_calls and "用户在 VS Code 里看代码" in text_calls[0]:
            print(f"  [PASS] screenshot observation embedded in user_text")
            passed += 1
        else:
            print(f"  [FAIL] observation missing from user_text")

    print(f"\n  Screenshot: {passed}/{total} passed")
    return passed, total


def test_chat_path_with_image_desc_text_only():
    """Chat trigger with an image: caller pre-describes the image and splices
    it into trigger_text as ``[图片：xxx]``. Memory tier never sees raw bytes.
    """
    print("\n" + "=" * 60)
    print("TEST: chat trigger — image_desc inlined → text-only path")
    print("=" * 60)

    from memory_prompts import call_miru_emotion_eval
    passed = 0
    total = 0

    text_calls = []

    def mock_text(system, user_text, **kwargs):
        text_calls.append(user_text)
        return {"mood": "好奇", "valence": 0.2, "arousal": 0.3, "reason": "看到图片"}

    trigger_text = "你看这个\n[图片：一只橘猫趴在键盘上，眼神慵懒]"

    with patch("prompt._call_retrieval_llm", side_effect=mock_text), \
         patch("prompt._call_llm_multimodal_json") as mock_mm:
        result = call_miru_emotion_eval(
            trigger_kind="chat",
            trigger_text=trigger_text,
            today_chat="",
            current_state={"mood": "neutral", "valence": 0, "arousal": 0.2},
            days_together=0,
        )

        total += 1
        if text_calls and not mock_mm.called:
            print(f"  [PASS] text-only path used (no multimodal)")
            passed += 1
        else:
            print(f"  [FAIL] expected text-only, got text={bool(text_calls)} mm={mock_mm.called}")

        total += 1
        if text_calls and "[图片：一只橘猫" in text_calls[0]:
            print(f"  [PASS] image_desc preserved in user_text")
            passed += 1
        else:
            print(f"  [FAIL] image_desc not preserved; text[0]={text_calls[0][:100] if text_calls else '<none>'}")

        total += 1
        if result and result["mood"] == "好奇":
            print(f"  [PASS] result returned correctly: mood={result['mood']}")
            passed += 1
        else:
            print(f"  [FAIL] result wrong: {result}")

    print(f"\n  ChatImageDesc: {passed}/{total} passed")
    return passed, total


def test_today_chat_image_placeholder_in_user_text():
    """When today_chat already contains [图片：xxx] placeholders (built by
    core._format_today_chat_for_miru_emotion using msg.image_desc),
    they must be forwarded verbatim to the LLM."""
    print("\n" + "=" * 60)
    print("TEST: today_chat [图片：xxx] placeholder forwarded as-is")
    print("=" * 60)

    from memory_prompts import call_miru_emotion_eval
    passed = 0
    total = 0
    captured = []

    def mock_text(system, user_text, **kwargs):
        captured.append(user_text)
        return {"mood": "平静", "valence": 0.05, "arousal": 0.2, "reason": ""}

    with patch("prompt._call_retrieval_llm", side_effect=mock_text):
        call_miru_emotion_eval(
            trigger_kind="chat",
            trigger_text="你看",
            today_chat="  [12:00] 你：[图片：一只橘猫]\n  [12:05] Miru：好可爱呀",
            current_state={"mood": "neutral", "valence": 0, "arousal": 0.2},
            days_together=0,
        )

    total += 1
    if captured and "[图片]" in captured[0]:
        print(f"  [PASS] [图片] placeholder forwarded verbatim")
        passed += 1
    else:
        print(f"  [FAIL] [图片] placeholder missing from user_text")

    print(f"\n  Placeholder: {passed}/{total} passed")
    return passed, total


def main():
    results = []
    results.append(test_prompt_contains_soul_and_blocks())
    results.append(test_call_miru_emotion_eval_valid())
    results.append(test_call_miru_emotion_eval_invalid())
    results.append(test_screenshot_path_text_only())
    results.append(test_chat_path_with_image_desc_text_only())
    results.append(test_today_chat_image_placeholder_in_user_text())

    total_p = sum(r[0] for r in results)
    total_t = sum(r[1] for r in results)

    print("\n" + "=" * 60)
    print(f"TOTAL: {total_p}/{total_t} passed")
    print("=" * 60)

    if total_p < total_t:
        sys.exit(1)


if __name__ == "__main__":
    main()
