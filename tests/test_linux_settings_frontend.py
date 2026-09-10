import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_windows_capture_recovery_frontend import _function_source


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("platform,device", [
    ("linux", "这台 Linux 电脑"), ("windows", "这台 Windows 电脑"),
    ("macos", "这台 Mac"), ("android", "这部手机"),
])
@pytest.mark.parametrize("mode", ["local", "remote"])
def test_settings_copy_matches_device_and_storage_scope(platform, device, mode):
    html = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
    functions = "\n".join(_function_source(html, name) for name in (
        "settingsRenderAiPanel", "settingsRenderSystemPanel",
        "_loadSettingsInvitationCode", "settingsSwitchAccount",
    ))
    fixture = json.dumps({"platform": platform, "device": device, "mode": mode}, ensure_ascii=False)
    harness = "const fixture = " + fixture + ";\n" + r"""
const assert = require('node:assert/strict');
const window = { __MIRU_DESKTOP_PLATFORM__: fixture.platform };
if (fixture.platform === 'android') window.MiruAndroid = {};
const elements = Object.fromEntries([
  'settingsPanelAi', 'settingsPanelSystem', 'settingsInvitationCode',
  'settingsInvitationHint', 'settingsCopyInvitationBtn',
].map(id => [id, { innerHTML: '', textContent: '' }]));
const document = { getElementById: id => elements[id] || null };
const localStorage = { getItem: () => null, removeItem: () => {} };
const navigator = { platform: fixture.platform };
const _clientModeMode = fixture.mode;
const _isMobile = fixture.platform === 'android';
const appConfigState = {};
const settingsAiConfigState = {};
const SETTINGS_AI_TIER_META = { vision: {}, chat: {}, memory: {} };
const _settingsAiTier = () => ({});
const _settingsAiStatusText = () => '';
const _settingsEsc = String;
const escapeHtml = String;
const _formatHotkeyDisplay = String;
const _updateAndroidCaptureUI = () => {};
const _loadScreenshotStats = () => {};
const fetch = async () => ({ ok: true, json: async () => (
  fixture.mode === 'local' ? { local_only: true } : { ok: true, invitation_code: 'fixture-code' }
) });
let confirmation;
const _confirm = async message => { confirmation = message; return false; };
""" + functions + r"""
(async () => {
  settingsRenderAiPanel();
  settingsRenderSystemPanel();
  await new Promise(resolve => setImmediate(resolve));
  await settingsSwitchAccount();
  const ai = elements.settingsPanelAi.innerHTML;
  const account = elements.settingsPanelSystem.innerHTML;
  const invitation = elements.settingsInvitationHint.textContent;
  if (fixture.mode === 'local') {
    const device = fixture.device + (fixture.platform === 'macos' ? ' ' : '');
    assert.ok(ai.includes('这些配置保存在' + device + '中。'), 'AI storage scope');
    assert.ok(account.includes('不会删除' + device + '上的本地 Miru 数据'), 'account description');
    assert.ok(invitation.includes('只在' + device + '上使用'), 'local invitation hint');
    assert.ok(confirmation.includes('数据会继续留在' + device + '上'), 'switch confirmation');
    if (fixture.platform === 'linux') {
      assert.ok(confirmation.includes('“只在这台电脑使用”'));
      for (const copy of [ai, account, invitation, confirmation]) assert.ok(!copy.includes('Mac'));
    }
  } else {
    assert.ok(ai.includes('当前 Miru 私有服务器中'));
    assert.ok(account.includes('服务器里的 Miru 数据不会被删除'));
    assert.equal(elements.settingsInvitationCode.value, 'fixture-code');
    assert.ok(confirmation.includes('不会删除服务器上的聊天、记忆或邀请码'));
  }
  console.log(JSON.stringify({ ok: true }));
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
"""
    result = subprocess.run(["node"], input=harness, capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
