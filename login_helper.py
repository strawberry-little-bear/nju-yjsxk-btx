"""登录辅助模块。

功能：
- 独立运行：自动填账号密码 -> 截取验证码 -> captcha_ocr 自动识别 -> 填入并登录；
  识别失败或验证码错误时自动刷新重试，全程无人值守。
- 可被主脚本复用：perform_login() 接受外部传入的 driver，登录成功后返回同一个
  driver，供主脚本继续进入课程页选课。

验证码来源 captcha_provider(image_path) -> str：
- 默认 auto_captcha_provider：调用 captcha_ocr.recognize_image 自动识别。
- 可传 prompt_captcha 改为人工输入，或自定义识别函数：
      def my_ocr(image_path): ...
      perform_login(driver, config, captcha_provider=my_ocr)
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait


DEFAULT_CAPTCHA_DIR = Path("captcha_screenshots")
CAPTCHA_IMAGE_NAME = "1.png"

HOME_TEXT_MARKERS = ("我的选课", "退出登录", "安全退出", "注销", "个人信息")

# 登录失败提示词（不同站点措辞不同，按需扩充）
LOGIN_FAILURE_MARKERS = (
    "验证码错误",
    "验证码不正确",
    "验证码输入错误",
    "验证码有误",
    "验证码无效",
    "验证码过期",
    "账号或密码错误",
    "用户名或密码错误",
    "密码错误",
    "登录失败",
    "账号已锁定",
    "账户已锁定",
    "账户被锁",
)


def build_driver():
    options = Options()
    profile_dir = Path.cwd() / f".chrome-profile-{datetime.now():%Y%m%d-%H%M%S-%f}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    cached_driver = (
        Path.home()
        / ".wdm/drivers/chromedriver/win64/152.0.7977.82/chromedriver-win64/chromedriver.exe"
    )
    if cached_driver.exists():
        driver = webdriver.Chrome(service=Service(str(cached_driver)), options=options)
    else:
        driver = webdriver.Chrome(options=options)
    driver.set_window_size(1200, 900)
    return driver


def print_timestamp(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def read_config(path: str | Path = "config.json") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"缺少配置文件：{config_path}")
    with config_path.open(encoding="utf-8") as file:
        config = json.load(file)
    if "url" not in config or "UserId" not in config or "PassWd" not in config:
        raise ValueError("config.json 中缺少 url / UserId / PassWd")
    return config


# ---------------------------------------------------------------------------
# 验证码截图
# ---------------------------------------------------------------------------

def locate_captcha_image(driver):
    candidates = [
        (By.ID, "vcodeImg"),
        (By.CSS_SELECTOR, "img[id='vcodeImg']"),
        (By.CSS_SELECTOR, "img[src*='vcode']"),
        (By.CSS_SELECTOR, "img[src*='image.do']"),
        (By.ID, "captchaimg"),
        (By.CSS_SELECTOR, "img[id='captchaimg']"),
        (By.CSS_SELECTOR, "img[src*='captcha']"),
        (By.CSS_SELECTOR, "img[src*='verify']"),
        (By.XPATH, "//img[contains(@src, 'captcha') or contains(@src, 'verify')]"),
    ]
    for by, value in candidates:
        try:
            element = driver.find_element(by, value)
        except NoSuchElementException:
            continue
        if element.is_displayed():
            return element
    return None


def save_captcha_screenshot(driver, output_path: Path) -> Path | None:
    element = locate_captcha_image(driver)
    if element is None:
        print_timestamp("[截图] 未找到显示的验证码图片。")
        return None

    try:
        screenshot = element.screenshot_as_png
    except Exception as exc:  # pragma: no cover - browser dependent
        print_timestamp(f"[截图] 截取验证码失败：{exc}")
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(screenshot)
    print_timestamp(f"[截图] 验证码截图已保存到：{output_path}")
    return output_path


def capture_captcha(driver, captcha_dir: Path = DEFAULT_CAPTCHA_DIR) -> Path | None:
    """截图验证码并保存为 captcha_dir/1.png（固定文件名，覆盖上一次）。"""
    captcha_dir = Path(captcha_dir)
    screenshot_path = captcha_dir / CAPTCHA_IMAGE_NAME
    result = save_captcha_screenshot(driver, screenshot_path)
    return screenshot_path if result is not None else None


# ---------------------------------------------------------------------------
# 登录流程
# ---------------------------------------------------------------------------
def fill_credentials(driver, config, timeout: int = 300):
    """打开登录页并自动填写账号密码。"""
    driver.get(config["url"])
    wait = WebDriverWait(driver, timeout)
    user_input = wait.until(ec.element_to_be_clickable((By.ID, "loginName")))
    password_input = wait.until(ec.element_to_be_clickable((By.ID, "loginPwd")))
    user_input.clear()
    user_input.send_keys(config["UserId"])
    password_input.clear()
    password_input.send_keys(config["PassWd"])
    print_timestamp("已填写账号密码，准备保存验证码截图。")
    return wait


def find_login_button(driver):
    """查找真正的登录按钮，避免点到“请使用…登录”之类的提示文本。"""
    candidates = [
        (By.ID, "login"),
        (By.ID, "loginButton"),
        (By.ID, "btnLogin"),
        (By.CSS_SELECTOR, "input[type='submit'], input[type='button']"),
        (By.CSS_SELECTOR, "button[type='submit'], button"),
        (By.CSS_SELECTOR, "a#login, a.login-btn, a.btn-login, a.loginBtn, a.btnLogin"),
        (By.XPATH, "//a[normalize-space(.)='登录' or normalize-space(.)='登 录']"),
        (By.XPATH, "//button[normalize-space(.)='登录' or normalize-space(.)='登 录']"),
        (By.XPATH, "//input[normalize-space(@value)='登录' or normalize-space(@value)='登 录']"),
    ]
    for by, value in candidates:
        try:
            elements = driver.find_elements(by, value)
        except NoSuchElementException:
            continue
        for element in elements:
            try:
                if not (element.is_displayed() and element.is_enabled()):
                    continue
                text = " ".join((element.text or "").split())
                # 有文字但不是“登录”字样的一律跳过（如“请使用本人统一身份认证账号登录”）
                if text and not re.match(r"^登\s*录$", text):
                    continue
                return element
            except Exception:
                continue
    return None


def close_visible_popups(driver):
    """关闭当前页面上可见的弹窗（JS alert / DOM 弹窗 / iframe 内弹窗），返回关闭数量。"""
    closed = 0

    # 1) JS alert / confirm 弹窗（会阻塞页面交互，必须优先处理）
    try:
        alert = driver.switch_to.alert
        alert_text = alert.text or ""
        alert.accept()
        print_timestamp(f"[弹窗] 已关闭 JS 弹窗：{alert_text[:80]}")
        closed += 1
        time.sleep(0.3)
    except Exception:
        pass

    close_selector = (
        ".zeromodal [class*='close'], .zeromodal-close, "
        ".modal [class*='close'], .modal-close, "
        ".layui-layer [class*='close'], .layui-layer-close, .layui-layer-close2, "
        ".el-dialog [class*='close'], .el-dialog__headerbtn, "
        ".ant-modal [class*='close'], .ant-modal-close, "
        ".ui-dialog [class*='close'], .ui-dialog-titlebar-close, "
        "[aria-label='关闭'], [title='关闭'], [aria-label='Close'], [title='Close']"
    )
    confirm_xpath = (
        "//div[contains(@class,'modal') or contains(@class,'dialog') "
        "or contains(@class,'layui-layer')]//*[self::button or self::a or @role='button']"
        "[contains(normalize-space(.),'确定') or contains(normalize-space(.),'知道了') "
        "or contains(normalize-space(.),'关闭') or contains(normalize-space(.),'取消')]"
    )

    def _click_closeables(within_driver):
        count = 0
        for element in within_driver.find_elements(By.CSS_SELECTOR, close_selector):
            if element.is_displayed():
                try:
                    within_driver.execute_script("arguments[0].click();", element)
                    count += 1
                except Exception:
                    pass
        for element in within_driver.find_elements(By.XPATH, confirm_xpath):
            if element.is_displayed():
                try:
                    within_driver.execute_script("arguments[0].click();", element)
                    count += 1
                except Exception:
                    pass
        return count

    # 2) 当前页面直接搜关闭按钮
    closed += _click_closeables(driver)
    if closed:
        time.sleep(0.5)
        return closed

    # 3) 兜底：弹窗在 iframe 里
    for iframe in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.frame(iframe)
            inner_closed = _click_closeables(driver)
            if inner_closed:
                time.sleep(0.5)
                closed += inner_closed
        except Exception:
            pass
        finally:
            driver.switch_to.default_content()
    return closed


def fill_captcha_and_submit(driver, captcha_value: str, auto_click: bool = True) -> None:
    captcha_inputs = driver.find_elements(By.ID, "verifyCode")
    if not captcha_inputs:
        captcha_inputs = driver.find_elements(By.ID, "loginCode")
    if not captcha_inputs:
        raise RuntimeError("未找到验证码输入框。")
    captcha_input = captcha_inputs[0]
    captcha_input.clear()
    captcha_input.send_keys(captcha_value)
    print_timestamp("已填入验证码。")
    close_visible_popups(driver)

    def press_enter():
        inputs = driver.find_elements(By.ID, "verifyCode")
        if not inputs:
            inputs = driver.find_elements(By.ID, "loginCode")
        if inputs:
            try:
                inputs[0].send_keys(Keys.RETURN)
                return True
            except Exception as exc:
                print_timestamp(f"[登录] 按回车提交失败：{exc}")
        return False

    try:
        login_button = find_login_button(driver)
    except Exception as exc:
        print_timestamp(f"[登录] 查找登录按钮异常，改为按回车提交：{exc}")
        login_button = None

    if login_button is not None:
        try:
            for attempt in range(1, 4):
                if attempt > 1:
                    login_button = find_login_button(driver)
                    if login_button is None:
                        break
                try:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", login_button
                    )
                    login_button.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", login_button)
                    except Exception:
                        pass
                print_timestamp("已点击登录按钮；等待登录结果。")
                time.sleep(1.0)
                closed = close_visible_popups(driver)
                if closed == 0:
                    return
                print_timestamp(
                    f"[登录] 点击登录后出现弹窗，已自动关闭（第 {attempt} 次），重新点击登录。"
                )
            return
        except Exception as exc:
            print_timestamp(f"[登录] 点击登录按钮异常，改为按回车提交：{exc}")

    if not auto_click:
        print_timestamp("未自动点击登录按钮；请手动点击登录后按回车继续。")
        input("登录完成后按回车继续...")
        return

    # 找不到按钮或点击异常：直接在验证码输入框按回车提交表单
    print_timestamp("[登录] 改为按回车提交登录。")
    for attempt in range(1, 4):
        press_enter()
        print_timestamp("已按回车提交登录；等待登录结果。")
        time.sleep(1.0)
        closed = close_visible_popups(driver)
        if closed == 0:
            return
        print_timestamp(
            f"[登录] 提交后出现弹窗，已自动关闭（第 {attempt} 次），再次按回车。"
        )


def prompt_captcha(image_path) -> str:
    print_timestamp("请查看本地验证码截图后，在此输入验证码并按回车继续。")
    try:
        return input("验证码：").strip()
    except KeyboardInterrupt as exc:
        raise SystemExit("用户中断输入，退出登录辅助流程。") from exc


def auto_captcha_provider(image_path) -> str:
    """默认识别钩子：调用 captcha_ocr 模块自动识别验证码。"""
    from captcha_ocr import recognize_image

    result = recognize_image(image_path)
    print_timestamp(f"[OCR] 识别结果：{result or '(空)'}")
    return result


def refresh_captcha(driver, cooldown: float = 1.0) -> None:
    """点击验证码图片刷新，并清空验证码输入框。"""
    element = locate_captcha_image(driver)
    if element is not None:
        try:
            driver.execute_script("arguments[0].click();", element)
        except Exception as exc:
            print_timestamp(f"[刷新] 点击验证码图片未生效：{exc}")
    for selector in ("#verifyCode", "#loginCode"):
        for captcha_input in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                captcha_input.clear()
            except Exception:
                pass
    time.sleep(cooldown)


# ---------------------------------------------------------------------------
# 登录检测
# ---------------------------------------------------------------------------
def _login_observation(driver):
    try:
        visible_login_fields = [
            element
            for element in driver.find_elements(By.ID, "loginName")
            + driver.find_elements(By.ID, "loginPwd")
            if element.is_displayed()
        ]
    except (NoSuchElementException, TimeoutException):
        return -1, None, False
    body_text = " ".join(driver.find_element(By.TAG_NAME, "body").text.split())
    matched = next((marker for marker in HOME_TEXT_MARKERS if marker in body_text), None)
    structural_markers = {
        "course_link": len(driver.find_elements(By.CSS_SELECTOR, "a[href*='course_nju']")),
        "course_container": len(driver.find_elements(By.ID, "xkTabContainer")),
    }
    has_home_structure = bool(matched or any(structural_markers.values()))
    return len(visible_login_fields), matched, has_home_structure


def _login_succeeded_now(driver) -> bool:
    field_count, _, has_home_structure = _login_observation(driver)
    return field_count == 0 and has_home_structure


def detect_login_failure(driver):
    """页面出现登录失败提示时返回对应提示词，否则返回 None。"""
    try:
        body_text = " ".join(driver.find_element(By.TAG_NAME, "body").text.split())
    except NoSuchElementException:
        return None
    return next((marker for marker in LOGIN_FAILURE_MARKERS if marker in body_text), None)


def wait_after_submit(driver, wait_seconds: float = 8.0, stable_seconds: float = 4.0):
    """提交登录后的短窗口检测。

    返回：
    - "success"：检测到登录成功并保持稳定
    - 其他字符串：页面出现的失败提示词（如"验证码错误"）
    - "unknown"：窗口内未确认成功也未确认失败
    """
    stable_since = None
    end_time = time.monotonic() + wait_seconds
    while time.monotonic() < end_time:
        close_visible_popups(driver)
        failure_marker = detect_login_failure(driver)
        if failure_marker:
            print_timestamp(f"[登录检测] 页面提示：{failure_marker}")
            return failure_marker
        if _login_succeeded_now(driver):
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_seconds:
                return "success"
        else:
            stable_since = None
        time.sleep(1)
    return "unknown"


def wait_for_login_success(driver, timeout: int = 300) -> None:
    def login_state(current_driver):
        field_count, marker, has_home_structure = _login_observation(current_driver)
        try:
            body_preview = " ".join(current_driver.find_element(By.TAG_NAME, "body").text.split())[:180]
        except NoSuchElementException:
            body_preview = ""
        return field_count, marker, has_home_structure, current_driver.current_url, body_preview

    stable_success_count = 0
    end_time = time.monotonic() + timeout
    while time.monotonic() < end_time:
        try:
            field_count, marker, has_home_structure, current_url, body_preview = login_state(driver)
        except (NoSuchElementException, TimeoutException):
            field_count, marker, has_home_structure = -1, None, False
            current_url, body_preview = driver.current_url, ""
        print_timestamp(
            f"[登录检测] url={current_url} 可见登录框={field_count} "
            f"首页文字特征={marker or '未发现'} 首页结构={has_home_structure} "
            f"页面={body_preview!r}"
        )
        if field_count == 0 and has_home_structure:
            stable_success_count += 1
            if stable_success_count >= 2:
                return
        else:
            stable_success_count = 0
        time.sleep(2)

    raise TimeoutException("等待登录成功超时：仍未检测到登录后的首页菜单。")


def perform_login(
    driver,
    config,
    timeout: int = 300,
    captcha_provider=None,
    captcha_dir: Path = DEFAULT_CAPTCHA_DIR,
    max_attempts: int = 6,
    post_submit_wait: float = 8.0,
    between_attempts: float = 2.0,
):
    """自动登录：识别验证码 -> 提交 -> 失败自动刷新重试，成功后返回同一个 driver。

    captcha_provider: (image_path) -> 验证码字符串；默认 auto_captcha_provider(OCR)。
    也可传 prompt_captcha（人工输入）或直接传字符串验证码（便于测试）。
    max_attempts: 最大尝试次数；post_submit_wait: 提交后检测窗口秒数；
    between_attempts: 每次失败刷新后的等待秒数。
    """
    fill_credentials(driver, config, timeout)
    provider = auto_captcha_provider if captcha_provider is None else captcha_provider

    for attempt in range(1, max_attempts + 1):
        print_timestamp(f"[登录尝试] 第 {attempt}/{max_attempts} 次")
        screenshot_path = capture_captcha(driver, captcha_dir)
        if screenshot_path is None:
            raise RuntimeError("验证码截图失败，无法继续登录。")

        captcha_value = (
            provider(str(screenshot_path)) if callable(provider) else str(provider)
        )
        captcha_value = str(captcha_value).strip().upper()
        if not captcha_value:
            print_timestamp("[登录尝试] 验证码识别为空，刷新验证码后重试。")
            refresh_captcha(driver, cooldown=between_attempts)
            continue

        print_timestamp(f"[登录尝试] 验证码：{captcha_value}")
        fill_captcha_and_submit(driver, captcha_value, auto_click=True)
        outcome = wait_after_submit(driver, wait_seconds=post_submit_wait)
        if outcome == "success":
            print_timestamp(f"登录成功。当前URL={driver.current_url}")
            return driver

        print_timestamp(f"[登录尝试] 本次未成功（{outcome}），刷新验证码重试。")
        refresh_captcha(driver, cooldown=between_attempts)

    raise TimeoutException(
        f"连续 {max_attempts} 次登录尝试均未成功，请检查账号密码或验证码识别效果。"
    )


def main(
    config_path: str = "config.json",
    captcha_dir: Path = DEFAULT_CAPTCHA_DIR,
    timeout: int = 300,
    manual_captcha: bool = False,
    max_attempts: int = 6,
    post_submit_wait: float = 8.0,
    between_attempts: float = 2.0,
) -> None:
    config = read_config(config_path)
    driver = build_driver()
    try:
        try:
            perform_login(
                driver,
                config,
                timeout=timeout,
                captcha_dir=captcha_dir,
                captcha_provider=prompt_captcha if manual_captcha else None,
                max_attempts=max_attempts,
                post_submit_wait=post_submit_wait,
                between_attempts=between_attempts,
            )
            print_timestamp(f"页面标题={driver.title}")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print_timestamp(f"[错误] 登录流程异常：{exc}")
    finally:
        print_timestamp("登录辅助浏览器已结束；按回车关闭窗口。")
        try:
            input("按回车关闭浏览器...")
        except Exception:
            pass
        driver.quit()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="登录辅助：自动识别验证码并登录，失败自动重试")
    parser.add_argument("--capture-only", action="store_true", help="只截图验证码并退出，不输入验证码")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--timeout", type=int, default=300, help="登录等待超时秒数")
    parser.add_argument("--manual", action="store_true", help="人工输入验证码（默认自动 OCR 识别）")
    parser.add_argument("--max-attempts", type=int, default=6, help="自动登录最大尝试次数")
    parser.add_argument("--post-submit-wait", type=float, default=8.0, help="点击登录后检测窗口秒数")
    parser.add_argument("--between-attempts", type=float, default=2.0, help="每次失败后刷新等待秒数")
    args = parser.parse_args()


    if args.capture_only:
        config = read_config(args.config)
        driver = build_driver()
        try:
            fill_credentials(driver, config, args.timeout)
            result = capture_captcha(driver)
            if result is not None:
                print_timestamp("截图完成，2 秒后自动退出。")
                time.sleep(2)
            else:
                print_timestamp("未截到验证码图片，截图失败。")
        finally:
            driver.quit()
    else:
        main(
            config_path=args.config,
            timeout=args.timeout,
            manual_captcha=args.manual,
            max_attempts=args.max_attempts,
            post_submit_wait=args.post_submit_wait,
            between_attempts=args.between_attempts,
        )
