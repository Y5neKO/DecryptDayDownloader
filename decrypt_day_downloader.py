#!/usr/bin/env python3
"""
DecryptDay 文件下载器
获取文件列表并显示下载链接

注意: /app/{id}?/files 接口的 app 归属由 POST body 里的 CBOR payload 决定
（结构: {"appId": "<内部ID>", "version": "<版本>", "isPremier": undefined}），
URL 里的 app id 仅用于 Referer/路由。payload 必须按目标应用动态生成，
否则无论输入什么 app id 都会返回同一个应用的文件。

cf_clearance 自动化: 优先使用 Playwright 驱动本机真实 Chrome（headful），
自动等待 Cloudflare 验证通过并读取 cf_clearance + UA + 页面 HTML；
浏览器档案持久化在 .cf_profile/，有效期内的重复运行无需再过验证。
未安装 playwright 时回退到手动粘贴流程。
"""

import re
import time
import webbrowser
import subprocess
import json
from pathlib import Path
from typing import Optional, List

import requests

# 站点版本号，来自原始抓包 payload（version 字段），站点升级后可能需要更新
SITE_VERSION = "6.4.0"

# 浏览器档案目录（持久化 cf_clearance，复用免验证）
PROFILE_DIR = Path(__file__).parent / ".cf_profile"
# CDP 直链获取用的独立 Chrome 档案（Chrome 136+ 禁止默认 profile 开调试端口）
CDP_PROFILE_DIR = Path(__file__).parent / ".chrome_cdp_profile"
CDP_ENDPOINT = "http://127.0.0.1:9222"


def _cdp_alive(timeout: float = 3.0) -> bool:
    """探测 CDP 端口是否可用"""
    try:
        return requests.get(f"{CDP_ENDPOINT}/json/version", timeout=timeout).ok
    except Exception:
        return False


def _launch_debug_chrome() -> bool:
    """启动带调试端口的独立 Chrome 实例"""
    import sys
    import time as _time

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome Beta",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    binary = next((b for b in candidates if Path(b).exists()), None)
    if not binary:
        print(f"未找到 Chrome（{sys.platform}），请手动启动:")
        print(f'  "<Chrome路径>" --remote-debugging-port=9222 --user-data-dir="{CDP_PROFILE_DIR}"')
        return False

    print("正在启动带调试端口的 Chrome 实例...")
    try:
        subprocess.Popen(
            [binary,
             f"--remote-debugging-port=9222",
             f"--user-data-dir={CDP_PROFILE_DIR}",
             "--no-first-run",
             "--no-default-browser-check",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        print(f"启动失败: {e}")
        return False

    for _ in range(15):
        if _cdp_alive():
            print("CDP 端口就绪")
            return True
        _time.sleep(1)
    print("Chrome 已启动但 CDP 端口未就绪")
    return False


def get_user_input(prompt: str) -> str:
    """获取用户输入"""
    return input(prompt).strip()


def open_url(url: str) -> None:
    """在浏览器中打开URL"""
    print(f"正在打开浏览器: {url}")
    webbrowser.open(url)


def encode_cbor_payload(internal_app_id: str, version: str = SITE_VERSION) -> str:
    """
    构造 CBOR 载荷并返回 CSV 字节串形式

    复现站点前端行为: {"appId": ..., "version": <选中的版本名>, "isPremier": undefined}
    -> CBOR (map3) -> 字节以十进制逗号分隔提交
    """
    def enc_str(s: str) -> bytes:
        b = s.encode()
        if len(b) < 24:
            return bytes([0x60 + len(b)]) + b  # fixstr
        return bytes([0x78, len(b)]) + b      # text string, 1字节长度

    payload = (
        bytes([0xA3])                       # map, 3 对
        + enc_str("appId") + enc_str(internal_app_id)
        + enc_str("version") + enc_str(version)
        + enc_str("isPremier") + bytes([0xF7])  # undefined
    )
    return ",".join(str(b) for b in payload)


def solve_with_browser(app_id: str, wait_seconds: int = 180) -> Optional[dict]:
    """
    Playwright 驱动本机真实 Chrome 自动过 Cloudflare

    返回: {"cf_clearance": ..., "user_agent": ..., "html": ...} 或 None
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright，回退手动流程")
        return None

    page_url = f"https://decrypt.day/app/{app_id}"
    result = None

    with sync_playwright() as p:
        # 优先用本机真实 Chrome（指纹更干净），失败则用 playwright 自带 chromium
        try:
            context = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            context = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )

        try:
            page = context.pages[0] if context.pages else context.new_page()
            print(f"正在打开浏览器: {page_url}")
            print("如出现 Cloudflare 验证请在窗口中点击通过，脚本会自动检测...")
            page.goto(page_url, wait_until="domcontentloaded")

            # 轮询等待 cf_clearance 出现（持久化档案有效期内会立即命中）
            cf_clearance = None
            for _ in range(wait_seconds):
                cookies = context.cookies("https://decrypt.day")
                cf_clearance = next((c["value"] for c in cookies if c["name"] == "cf_clearance"), None)
                if cf_clearance:
                    break
                page.wait_for_timeout(1000)

            if not cf_clearance:
                print(f"等待 {wait_seconds}s 未获得 cf_clearance")
                return None

            user_agent = page.evaluate("navigator.userAgent")
            html = page.content()
            result = {
                "cf_clearance": cf_clearance,
                "user_agent": user_agent,
                "html": html,
            }
        finally:
            context.close()

    return result


def _looks_like_challenge(html: str) -> bool:
    """识别 Cloudflare 挑战页"""
    return "Just a moment" in html or "challenges.cloudflare.com" in html


def http_get(url: str, user_agent: str, cf_clearance: str, referer: Optional[str] = None) -> Optional[str]:
    """
    GET 请求: curl_cffi（模拟 Chrome TLS 指纹） -> curl -> requests

    cf_clearance 校验可能包含 TLS 指纹，python-requests 的 JA3 会被直接 403，
    与之前 POST 走 curl 能通过的现象一致。
    """
    accept_language = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6,zh-TW;q=0.5"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": accept_language,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": user_agent,
        "Cookie": f"cf_clearance={cf_clearance}",
    }
    if referer:
        headers["Referer"] = referer

    # 方式1: curl_cffi（TLS 指纹最接近真实 Chrome，优先）
    try:
        from curl_cffi import requests as crequests
        response = crequests.get(url, headers=headers, cookies={"cf_clearance": cf_clearance},
                                 timeout=30, impersonate="chrome")
        if response.status_code == 200 and not _looks_like_challenge(response.text):
            return response.text
        print(f"curl_cffi 状态码: {response.status_code}"
              + ("（返回 Cloudflare 挑战页）" if _looks_like_challenge(response.text) else ""))
    except ImportError:
        pass
    except Exception as e:
        print(f"curl_cffi 出错: {e}")

    # 方式2: curl 子进程
    curl_cmd = ["curl", "-s", "-m", "30", url]
    for key, value in headers.items():
        curl_cmd += ["-H", f"{key}: {value}"]
    curl_cmd += ["-w", "\n__HTTP_CODE__%{http_code}"]
    try:
        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=35)
        if result.returncode == 0 and "__HTTP_CODE__" in result.stdout:
            body, _, code = result.stdout.rpartition("__HTTP_CODE__")
            if code == "200" and body and not _looks_like_challenge(body):
                return body
            print(f"curl 状态码: {code}"
                  + ("（返回 Cloudflare 挑战页）" if _looks_like_challenge(body) else ""))
        else:
            print(f"curl 执行失败: {result.stderr.strip()}")
    except Exception as e:
        print(f"curl 执行出错: {e}")

    # 方式3: requests 兜底
    print("正在尝试使用 requests 兜底...")
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200 and not _looks_like_challenge(response.text):
            return response.text
        print(f"requests 状态码: {response.status_code}"
              + ("（返回 Cloudflare 挑战页）" if _looks_like_challenge(response.text) else ""))
    except Exception as e:
        print(f"requests 出错: {e}")

    return None


def fetch_app_page(app_id: str, cf_clearance: str, user_agent: str) -> Optional[str]:
    """GET 应用页面 HTML，用于提取内部 appId"""
    url = f"https://decrypt.day/app/{app_id}"
    print(f"正在请求: {url}")
    return http_get(url, user_agent, cf_clearance, referer=url)


def extract_internal_ids(html: str, app_id: str) -> tuple:
    """
    从应用页面 HTML（SvelteKit 序列化数据）提取内部 ID

    应用主记录: app:{id:"<内部ID>",app_id:"id<数字>",...}（ID 格式不固定，
    老记录为 cuid 如 cl9se40t70055dofw1xo61mwx，新记录为 nanoid 如 p7-UcPBfHBDiVqQ0BZ6HF）
    版本记录: 闭包赋值 f.id="<版本ID>";f.name="1.0.0"

    返回: (应用主记录ID, [版本ID, ...])
    """
    full = app_id if app_id.lower().startswith("id") else "id" + app_id

    # 1) 主记录 id 紧邻 app_id 字段（id 在前，标准顺序）
    m = re.search(r'id:"([^"]+)",app_id:"' + re.escape(full) + '"', html)
    app_internal = m.group(1) if m else None

    # 2) 字段顺序颠倒时，从 app_id 位置向前找最近的 id:"..."
    if not app_internal:
        pos = html.find(f'app_id:"{full}"')
        if pos > 0:
            before = html[max(0, pos - 400):pos]
            ids = re.findall(r'id:"([^"]+)"', before)
            if ids:
                app_internal = ids[-1]

    # 版本记录: 闭包赋值 x.id="...";x.name="<版本名>";x.release_notes=...
    # name 为纯数字点分形式才认定为版本名
    pairs = re.findall(r'[a-z]\.id="([^"]{10,40})";[a-z]\.name="([^"]*)"', html)
    version_ids, version_names = [], []
    for vid, vname in pairs:
        if re.match(r'^\d+(\.\d+)*$', vname):
            version_ids.append(vid)
            version_names.append(vname)
    # 去重保序
    version_ids = list(dict.fromkeys(version_ids))
    version_names = list(dict.fromkeys(version_names))

    return app_internal, version_ids, version_names


def extract_file_ids(response_data: dict) -> List[dict]:
    """
    从API响应中提取文件ID和存储类型

    返回: [{"id": "xxx", "storage": "drive.google.com"}, ...]
    """
    files = []

    try:
        raw_data = response_data.get("data", "")
        if isinstance(raw_data, str):
            data = json.loads(raw_data)
        else:
            data = raw_data

        if len(data) > 3 and isinstance(data[3], dict):
            files_info = data[3]
            files_index = files_info.get("files")

            if isinstance(files_index, int):
                file_indices = data[files_index] if files_index < len(data) else []
            elif isinstance(files_index, list):
                file_indices = files_index
            else:
                file_indices = []

            for idx in file_indices:
                if isinstance(idx, int) and idx < len(data) and isinstance(data[idx], dict):
                    file_obj = data[idx]

                    # 获取文件ID（每轮迭代重置，避免上一轮的值泄漏到本轮）
                    actual_id = None
                    id_index = file_obj.get("id")
                    if isinstance(id_index, int) and id_index < len(data) and isinstance(data[id_index], str):
                        actual_id = data[id_index]

                    # 获取存储类型
                    storage_index = file_obj.get("storage_provider")
                    storage = data[storage_index] if isinstance(storage_index, int) and storage_index < len(data) else "unknown"

                    if actual_id is not None:
                        files.append({"id": actual_id, "storage": storage})
                        print(f"  找到文件: {actual_id} ({storage})")
                    else:
                        print(f"  [!] 条目 {idx} 缺少有效的文件ID索引: {file_obj}")
    except Exception as e:
        print(f"解析响应数据时出错: {e}")

    if not files and isinstance(data, list):
        # 结构提示: 打印顶层元素类型与长度，便于分析 devalue 结构
        shape = ", ".join(
            f"[{i}]{type(v).__name__}(len={len(v)})" if isinstance(v, (list, dict, str))
            else f"[{i}]{type(v).__name__}({v!r})"
            for i, v in enumerate(data[:12])
        )
        print(f"  [!] 响应顶层结构: {shape}")

    return files


def fetch_file_list(app_id: str, cf_clearance: str, user_agent: str, form_data: str) -> Optional[dict]:
    """发送 POST 请求获取文件列表（优先 curl，失败则用 requests）"""
    url = f"https://decrypt.day/app/{app_id}?/files"

    print(f"正在请求: {url}")

    # 方式1: 使用 curl
    curl_cmd = [
        "curl", "-X", "POST", url,
        "-H", "Accept: */*",
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6,zh-TW;q=0.5",
        "-H", "Cache-Control: no-cache",
        "-H", "Pragma: no-cache",
        "-H", "Origin: https://decrypt.day",
        "-H", f"Referer: https://decrypt.day/app/{app_id}",
        "-H", f"User-Agent: {user_agent}",
        "-H", f"Cookie: cf_clearance={cf_clearance}",
        "-F", f"data={form_data}",
    ]

    try:
        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=30)

        if result.returncode == 0 and result.stdout:
            return json.loads(result.stdout)
        else:
            print(f"curl 执行失败: {result.stderr}")
    except subprocess.TimeoutExpired:
        print("curl 请求超时")
    except FileNotFoundError:
        print("curl 命令未找到")
    except json.JSONDecodeError as e:
        print(f"curl 响应 JSON 解析失败: {e}")
    except Exception as e:
        print(f"curl 执行出错: {e}")

    # 方式2: curl_cffi（模拟 Chrome TLS 指纹）
    try:
        from curl_cffi import requests as crequests
        response = crequests.post(url, headers=headers, cookies=cookies,
                                  files={"data": (None, form_data)}, timeout=30, impersonate="chrome")
        if response.status_code == 200:
            return response.json()
        print(f"curl_cffi 响应状态码: {response.status_code}")
    except ImportError:
        pass
    except Exception as e:
        print(f"curl_cffi 出错: {e}")

    # 方式3: 使用 requests 兜底
    print("正在尝试使用 requests 备用方案...")

    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6,zh-TW;q=0.5",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Origin": "https://decrypt.day",
        "Referer": f"https://decrypt.day/app/{app_id}",
        "User-Agent": user_agent,
    }

    cookies = {"cf_clearance": cf_clearance}

    files = {"data": (None, form_data)}

    try:
        response = requests.post(url, headers=headers, cookies=cookies, files=files, timeout=30)

        if response.status_code == 200:
            return response.json()
        else:
            print(f"requests 响应状态码: {response.status_code}")
            return None

    except Exception as e:
        print(f"requests 请求出错: {e}")
        return None


def resolve_dl_direct_links(dl_tasks: list, cdp_endpoint: str = "http://127.0.0.1:9222", max_wait: int = 120) -> dict:
    """
    通过调试端口的本机 Chrome 自动获取直链（最小 CDP 客户端）

    只使用 Page/DOM/Input 三个域，不开启 Runtime.enable（Turnstile 会检测
    Runtime 域并静默拒绝出 token，playwright attach 必开该域因此不可用）。

    页面交互流程（服务端按 Referer 决定渲染 dl 面板，故导航必须带 Referer）:
    Get download link -> 转圈等 Turnstile token -> 按钮变 Download
    -> 再点击一次 -> window.open 直链新标签页。

    前置: Chrome 以 --remote-debugging-port=9222 --user-data-dir=<独立目录> 启动
    （Chrome 136+ 禁止默认 profile 开调试端口，脚本会自动拉起独立实例）。
    返回: {dl_url: 直链或 None}
    """
    import re as _re
    from websocket import create_connection  # pip install websocket-client

    results = {url: None for url, _ in dl_tasks}
    if not _cdp_alive() and not _launch_debug_chrome():
        return results

    class CDP:
        def __init__(self, ws_url):
            self.ws = create_connection(ws_url, timeout=30)
            self.mid = 0

        def send(self, method, params=None, timeout=30):
            self.mid += 1
            self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
            self.ws.settimeout(timeout)
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == self.mid:
                    return msg

        def close(self):
            try:
                self.ws.close()
            except Exception:
                pass

    def list_targets():
        return requests.get(f"{cdp_endpoint}/json/list", timeout=5).json()

    def find_free_page_tab():
        """找一个 decrypt.day 的 page tab（复用），否则取任意 page tab"""
        tabs = [t for t in list_targets() if t.get("type") == "page"]
        for t in tabs:
            if "decrypt.day" in t.get("url", ""):
                return t
        return tabs[0] if tabs else None

    def cdp_click(cdp, node_id):
        r = cdp.send("DOM.getBoxModel", {"nodeId": node_id})
        model = r.get("result", {}).get("model", {}).get("content")
        if not model:
            return False
        x = (model[0] + model[2]) / 2
        y = (model[1] + model[5]) / 2
        for args in [{"type": "mouseMoved", "x": x, "y": y},
                     {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                     {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}]:
            cdp.send("Input.dispatchMouseEvent", args)
            time.sleep(0.15)
        return True

    def btn_state(cdp):
        """返回按钮可见文本: 'Get download link' / 'Getting download link...' / 'Download' / None"""
        r = cdp.send("DOM.getDocument", {"depth": 8})
        root = r["result"]["root"]["nodeId"]
        r = cdp.send("DOM.querySelector", {"nodeId": root, "selector": "button.btn-download"})
        nid = r.get("result", {}).get("nodeId")
        if not nid or int(nid) == 0:
            return None, None
        r = cdp.send("DOM.getOuterHTML", {"nodeId": nid})
        html = r.get("result", {}).get("outerHTML", "")
        text = _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", html)).strip()
        return nid, text

    for dl_url, referer in dl_tasks:
        print(f"\n处理: {dl_url}")
        tab = find_free_page_tab()
        if not tab:
            print("  调试 Chrome 中无可用标签页")
            continue
        cdp = None
        direct = None
        try:
            cdp = CDP(tab["webSocketDebuggerUrl"])
            cdp.send("Page.enable")
            cdp.send("Page.navigate", {"url": dl_url, "referrer": referer})
            time.sleep(8)

            # 第一段: 等 Get download link 按钮出现并点击
            nid = None
            for i in range(30):
                nid, text = btn_state(cdp)
                if nid and "Get download link" in text:
                    break
                if nid and text == "Download":
                    break  # 直链已在缓存中，直接进入第二段
                nid = None
                time.sleep(1)
            if not nid:
                print("  下载按钮未出现（面板未渲染）")
                continue
            if not cdp_click(cdp, nid):
                print("  按钮点击失败")
                continue

            # 等待 token: 按钮转圈后变 Download
            state = None
            for i in range(max_wait):
                nid, text = btn_state(cdp)
                state = text
                if text == "Download":
                    break
                time.sleep(2)
            if state != "Download":
                print(f"  取直链未完成（按钮状态: {state!r}，可能需要登录或超过免费限额）")
                continue

            # 第二段: 点击 Download -> window.open 直链
            # 直链特征: 站内 presigned 下载路径 /fs/dl/，或任意外域存储 URL
            cdp_click(cdp, nid)
            deadline = time.time() + 30
            while time.time() < deadline and not direct:
                for t in list_targets():
                    u = t.get("url", "")
                    if t.get("type") == "page" and u.startswith("http"):
                        if "/fs/dl/" in u or "decrypt.day" not in u:
                            direct = u
                            # 关掉直链标签页（避免真的开始下载文件）
                            requests.get(f"{cdp_endpoint}/json/close/{t['id']}", timeout=5)
                            break
                time.sleep(1)

            if direct:
                print(f"  直链: {direct}")
                results[dl_url] = direct
            else:
                print("  未捕获直链标签页")
        except Exception as e:
            print(f"  处理异常: {str(e)[:150]}")
        finally:
            if cdp:
                cdp.close()
            # 让 tab 回到空白，避免残留页面影响下个文件
            try:
                t2 = find_free_page_tab()
                if t2:
                    requests.get(f"{cdp_endpoint}/json/close/{t2['id']}", timeout=5)
            except Exception:
                pass

    return results

def manual_cf_flow(app_id: str) -> Optional[dict]:
    """手动流程: 打开浏览器 -> 用户粘贴 cf_clearance -> requests 拉页面"""
    page_url = f"https://decrypt.day/app/{app_id}?/files"

    print(f"即将打开浏览器访问: {page_url}")
    print("请在浏览器中完成 Cloudflare 验证")

    input("按 Enter 键打开浏览器...")

    open_url(page_url)

    print("请在浏览器中按 F12 打开开发者工具")
    print("切换到 Application（应用） > Cookies > decrypt.day")
    print("注意: 必须复制名为 cf_clearance 的 Cookie 值，不要复制成其他 Cookie")
    print("User-Agent 在 Console 标签页输入 navigator.userAgent 回车获取")

    cf_clearance = get_user_input("\n请粘贴 cf_clearance 值: ")

    if not cf_clearance:
        print("错误: cf_clearance 不能为空")
        return None

    default_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    # cf_clearance 与浏览器UA绑定，UA 不一致会被 Cloudflare 拒绝
    user_agent = get_user_input("请粘贴浏览器 User-Agent（回车使用默认 Chrome/145 macOS）: ") or default_ua

    html = fetch_app_page(app_id, cf_clearance, user_agent)
    if not html:
        return None

    return {"cf_clearance": cf_clearance, "user_agent": user_agent, "html": html}


def main():
    print("=" * 50)
    print("DecryptDay 文件下载器")
    print("=" * 50)

    # Step 1: 获取应用ID
    print("\n[Step 1] 输入应用信息")
    app_id = get_user_input("请输入应用ID（如 id1467190251）: ")

    if not app_id:
        print("错误: 应用ID不能为空")
        return

    # Step 2: 过 Cloudflare（自动优先，手动兜底）
    print("\n[Step 2] Cloudflare 验证")
    session = solve_with_browser(app_id)
    if session is None:
        session = manual_cf_flow(app_id)
    if session is None:
        print("未获得有效会话，退出")
        return

    # Step 3: 提取应用内部 ID
    print("\n[Step 3] 提取应用内部 ID")
    app_internal, version_ids, version_names = extract_internal_ids(session["html"], app_id)
    if not app_internal and not version_ids:
        print("未能从页面中提取内部 ID，无法构造请求")
        return

    print(f"应用主记录 ID: {app_internal}")
    if version_ids:
        print(f"版本 ID: {', '.join(version_ids[:3])}")
    if version_names:
        print(f"版本名: {', '.join(version_names[:3])}")

    # Step 4: 构造 CBOR 载荷并获取文件列表
    # 组合重试: (主记录ID × 版本名) 优先，其次 (版本ID × 版本名)，最后回退硬编码站点版本
    print("\n[Step 4] 获取文件列表")
    attempts = []
    if app_internal:
        for name in version_names[:2]:
            attempts.append((app_internal, name))
        attempts.append((app_internal, SITE_VERSION))
    for vid in version_ids[:2]:
        if version_names:
            attempts.append((vid, version_names[0]))
        attempts.append((vid, SITE_VERSION))
    seen = set()
    attempts = [a for a in attempts if not (a in seen or seen.add(a))]

    response_data = None
    files = []
    for cand_id, cand_ver in attempts:
        print(f"尝试 appId={cand_id}, version={cand_ver}")
        form_data = encode_cbor_payload(cand_id, cand_ver)
        response_data = fetch_file_list(app_id, session["cf_clearance"], session["user_agent"], form_data)
        if not response_data:
            print("请求失败，换下一组候选")
            continue
        files = extract_file_ids(response_data)
        if files:
            break
        print("该组合无文件，换下一组候选")

    if not response_data:
        print("获取文件列表失败")
        return

    # 调试: 原始响应和页面 HTML 落盘，便于分析响应结构
    debug_page = Path("/tmp/decrypt_day_page.html")
    debug_resp = Path("/tmp/decrypt_day_response.json")
    debug_page.write_text(session["html"], encoding="utf-8")
    debug_resp.write_text(json.dumps(response_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"调试文件已保存: {debug_page} / {debug_resp}")

    # Step 5: 解析并显示文件列表
    print("\n[Step 5] 解析文件列表")
    files = extract_file_ids(response_data)

    if not files:
        print("未找到可下载的文件")
        return

    # Step 6: 显示下载链接
    print("\n" + "=" * 60)
    print("下载链接列表")
    print("=" * 60)

    referer = f"https://decrypt.day/app/{app_id}"

    dl_tasks = []
    for i, file_info in enumerate(files, 1):
        file_id = file_info["id"]
        storage = file_info["storage"]
        download_url = f"https://decrypt.day/app/{app_id}/dl/{file_id}"
        dl_tasks.append((download_url, referer))
        print(f"\n文件 {i}: {file_id}")
        print(f"  存储类型: {storage}")
        print(f"  下载链接: {download_url}")
        print(f"  Referer: {referer}")

    # Step 7: CDP 接管真实 Chrome 自动获取直链
    print("\n[Step 7] 自动获取最终下载直链")
    go = get_user_input("通过 CDP 接管本机 Chrome 自动获取直链？(Y/n): ")
    if go.strip().lower() in ("", "y", "yes"):
        results = resolve_dl_direct_links(dl_tasks)
        ok = {u: d for u, d in results.items() if d}
        if ok:
            print("\n" + "=" * 60)
            print("最终直链")
            print("=" * 60)
            for u, d in ok.items():
                print(f"\n{u}\n  -> {d}")
        else:
            print("\n未能自动获取直链。")
    else:
        print("跳过自动获取。")

    print("\n" + "=" * 60)
    print("提示: 直链为 presigned URL，有效期仅 5 分钟，获取后尽快下载:")
    print('  curl -L -o 文件名.ipa "<直链>"')
    print("未获取到直链时（Premier 文件/限额），可在浏览器手动打开下载链接（需携带 Referer）")
    print("=" * 60)


if __name__ == "__main__":
    main()
