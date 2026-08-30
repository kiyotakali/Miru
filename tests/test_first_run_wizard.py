import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOGIN_HTML = ROOT / "templates" / "login.html"


def test_first_run_wizard_exposes_expected_paths():
    html = LOGIN_HTML.read_text(encoding="utf-8")

    assert "Miru 要住在哪里" in html
    assert "只在这台电脑使用" in html
    assert "让 Miru 住在这台电脑" in html
    assert "本地单设备模式" not in html
    assert "适合先自己体验 Miru" not in html
    assert "开始本地使用" in html
    assert "/api/client/local/start" in html
    assert "view-api-setup" in html
    assert "<h1>API Key</h1>" in html
    assert "连接 Miru 的模型" not in html
    assert "先配置 API Key" not in html
    assert "Miru 需要视觉、聊天、记忆三组模型" in html
    assert "服务地址填 OpenAI-compatible Base URL" in html
    assert "误填到 /v1/chat/completions 时会自动改回 /v1" not in html
    assert 'body[data-view="api-setup"] .sub' in html
    assert 'body[data-view="api-setup"] .note' in html
    assert "apiVisionHost" in html
    assert "apiChatHost" in html
    assert "apiMemoryHost" in html
    assert "saveApiSetupAndEnter" in html
    assert "skipApiSetupAndEnter" not in html
    assert "apiSetupSkipBtn" not in html
    assert "暂时跳过" not in html
    assert "可以先跳过" not in html
    assert "completeClientSetup" in html
    assert "/api/client/setup/complete" in html
    assert "/api/owner/ai-config/' + encodeURIComponent(tier) + '/test" in html
    assert "模型配置已保存并测试通过，正在进入 Miru。" in html
    assert "请把视觉、聊天、记忆三组都填完整，并测试通过后进入 Miru。" in html
    assert "u.searchParams.set('api_setup', status)" in html
    assert "enterMiruFromApiSetup('configured')" in html
    assert "enterMiruFromApiSetup('skipped')" not in html
    assert "if (!payloads.length)" not in html
    assert "resumePendingApiSetup" in html
    assert "setup_complete !== false" in html
    assert "miru_api_setup_skipped" not in html
    assert "不需要邀请码" in html
    assert "手机暂时不会同步" in html
    assert "多设备同步使用" in html
    assert "我已有邀请码" in html
    assert "我还没有邀请码" in html
    assert "使用官方服务器" in html
    assert "使用自己的服务器" in html
    assert "/api/client/provision/self-server/create" in html
    assert "/api/client/provision/self-server/status/" in html
    assert "/api/auth/login" in html
    assert "pollInFlight" in html
    assert "resetSelfProgress" in html
    assert "sakura-spinner" in html
    assert "deploy-steps" in html
    assert "Miru 服务包" in html
    assert "selfImageTar" in html
    assert "FormData" in html
    assert "正在把 Miru 服务包传到你的服务器" in html
    assert "selfSuccess" in html
    assert "复制邀请码" in html
    assert "进入 Miru" in html
    assert "再创建一个 Miru" not in html
    assert "重新检查公网访问" in html
    assert "/api/client/provision/self-server/public-health/" in html
    assert "第一个空闲端口" in html
    assert "实际端口" in html
    assert "pollFailures" in html
    assert "selfRetryJobId" in html
    assert "selfHealthRetryJobId" in html
    assert "先不要重复创建" in html
    assert "重新检查状态" in html
    assert "刚才的任务" in html
    assert "e.status === 404" in html
    assert "刚才的创建已经中断，请重新创建一次" in html
    assert "自动登录" not in html
    assert "miru/server:0.2.0" in html
    assert "miru/server:0.1.0" not in html
    assert "ccr.ccs.tencentyun.com/contextlife/miru-server" not in html
    assert "首次连接会信任这台服务器的指纹" in html
    assert "云服务器登录页显示的用户名" in html
    assert "选择 SSH 私钥" in html
    assert "在桌面端输入 // 使用本机密钥" in html
    assert "免密 sudo" in html
    assert "系统管理员" in html
    assert "请输入 SSH 用户名" in html
    assert "ssh_user: valueOf('selfUser')" in html
    assert "ssh_user: valueOf('selfUser') || 'ubuntu'" not in html
    assert "ssh_user: valueOf('selfUser') || 'root'" not in html
    assert 'value="ubuntu"' not in html
    assert "云服务器 root 密码" not in html
    assert "例如 ubuntu" not in html
    assert 'placeholder="输入你的云服务器公网 IPv4"' in html
    assert 'placeholder="203.0.113.10"' not in html
    assert "直连里面编码的服务器" not in html
    assert "独立 Miru 实例" not in html
    assert "Miru Server 镜像包" not in html
    assert "Host 目录" not in html
    assert "实例起始端口" not in html
    assert "实例结束端口" not in html
    assert "部署脚本" not in html
    assert "discovery" not in html
    assert "单设备本地模式会在后续版本开放" not in html


def test_first_run_creation_paths_offer_api_setup_before_entering_app():
    html = LOGIN_HTML.read_text(encoding="utf-8")

    local_body = re.search(
        r"window\.startLocalMode = async function\(\) \{([\s\S]*?)\n  \};",
        html,
    )
    assert local_body, "startLocalMode missing"
    local_js = local_body.group(1)
    assert "if (!data.is_new)" in local_js
    assert (
        "window.location.href = withDesktopClientParams('/app?token=' + "
        "encodeURIComponent(data.token));"
    ) in local_js
    assert "beginApiSetup" in local_js

    enter_body = re.search(
        r"window\.enterSelfServer = async function\(\) \{([\s\S]*?)\n  \};",
        html,
    )
    assert enter_body, "enterSelfServer missing"
    assert "returnState: true" in enter_body.group(1)
    assert "setupComplete: false" in enter_body.group(1)
    assert "beginApiSetup" in enter_body.group(1)


def test_first_run_wizard_does_not_restore_legacy_discovery_login():
    html = LOGIN_HTML.read_text(encoding="utf-8")

    assert "mirulife.top" not in html
    assert "DISCOVERY_URL" not in html
    assert "DEFAULT_SERVER_URL" not in html
    assert "旧的 discovery 逻辑" not in html
    assert "MIRU-ABCDEF" not in html
    assert "Milestone" not in html
    assert "邀请码格式不对，请确认是否完整复制" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_first_run_wizard_inline_js_is_valid():
    html = LOGIN_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>", html)
    assert scripts
    for index, script in enumerate(scripts):
        proc = subprocess.run(
            [
                "node",
                "-e",
                "new Function(require('fs').readFileSync(0, 'utf8'));",
            ],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert proc.returncode == 0, (
            f"script {index} failed\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
