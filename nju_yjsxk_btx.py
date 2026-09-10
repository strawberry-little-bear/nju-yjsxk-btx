"""Monitor plan courses and submit course selections through the web UI."""

import argparse
import json
import random
import time
import traceback
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait


UNAVAILABLE_MARKERS = ("已满", "满额", "无余量", "不可选")
LOGIN_EXPIRED_MARKERS = (
    "未登录不能选课",
    "登录超时",
    "会话已过期",
    "登录已失效",
    "请重新登录",
    "重新登录",
)
MAX_CONSECUTIVE_RECOVERIES = 3
_LOG_FILE = None
STATE_PATH = Path("state.json")


def start_logging():
    global _LOG_FILE
    log_dir = Path("log")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    _LOG_FILE = log_path.open("w", encoding="utf-8")
    return log_path


def log(message):
    timestamped = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(timestamped, flush=True)
    if _LOG_FILE:
        _LOG_FILE.write(timestamped + "\n")
        _LOG_FILE.flush()


def close_logging():
    global _LOG_FILE
    if _LOG_FILE:
        _LOG_FILE.close()
        _LOG_FILE = None


def load_state(keywords):
    fresh_state = {
        "successful_courses": [],
        "pending_keywords": list(dict.fromkeys(keywords)),
        "last_failure_reason": "",
        "refresh_count": 0,
        "last_run_at": "",
    }
    if not STATE_PATH.exists():
        return fresh_state
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("[状态] state.json 无法读取，使用全新任务状态。")
        return fresh_state
    configured = list(dict.fromkeys(keywords))
    saved_pending = [item for item in state.get("pending_keywords", []) if item in configured]
    saved_successful = state.get("successful_courses", [])
    pending = (
        saved_pending
        if "pending_keywords" in state
        else configured
    )
    state.update(
        successful_courses=saved_successful,
        pending_keywords=pending,
        last_failure_reason=state.get("last_failure_reason", ""),
        refresh_count=int(state.get("refresh_count", 0)),
        last_run_at=state.get("last_run_at", ""),
    )
    return state


def save_state(state):
    state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def session_expired(driver):
    """判断当前页面是否处于登录失效状态。

    尽量不抛异常：页面正在跳转、出现 JS 弹窗或元素失效时，检测失败一律视为
    “暂未确认失效”，由监控循环的异常安全网兜底处理。
    """
    body_text = ""
    body_html = ""
    try:
        body_element = driver.find_element(By.TAG_NAME, "body")
        body_text = " ".join(body_element.text.split())
        body_html = " ".join((body_element.get_attribute("textContent") or "").split())
    except NoSuchElementException:
        return False
    except Exception:
        return False
    message_text = ""
    try:
        for element in driver.find_elements(By.ID, "course_msgDiv"):
            try:
                value = " ".join(
                    (element.text or element.get_attribute("textContent") or "").split()
                )
                if value:
                    message_text += " " + value
            except StaleElementReferenceException:
                continue
    except Exception:
        pass
    try:
        login_visible = any(
            element.is_displayed()
            for element in driver.find_elements(By.ID, "loginName")
        )
    except Exception:
        login_visible = False
    try:
        current_url = (driver.current_url or "").lower()
    except Exception:
        current_url = ""
    url_is_login = any(part in current_url for part in ("index_nju", "/login", "login.do"))
    haystack = " ".join((body_text + " " + body_html + " " + message_text).split())
    return (
        login_visible
        or url_is_login
        or any(marker in haystack for marker in LOGIN_EXPIRED_MARKERS)
    )


def handle_possible_alert(driver):
    """如果页面上有 JS alert/confirm，读取内容并确认关闭，返回弹窗文本（无则空串）。"""
    try:
        alert = driver.switch_to.alert
    except Exception:
        return ""
    try:
        text = (alert.text or "").strip()
    except Exception:
        text = ""
    try:
        alert.accept()
    except Exception:
        try:
            alert.dismiss()
        except Exception:
            pass
    if text:
        log(f"[弹窗] 检测到并已关闭 JS 弹窗：{text[:200]}")
    return text


def recover_session(driver, config, timeout, state):
    """Save the task, wait for a fresh manual login, and reopen the course page."""
    save_state(state)
    log("[会话] 检测到登录已失效，已保存 state.json。")
    log("[会话] 正在重新打开登录页；使用 login_helper 自动重新登录（OCR 识别验证码）。")
    login(driver, config, timeout)
    grid_id, button_selector = open_plan_courses(driver, timeout)
    log("[会话] 重新登录成功，已恢复课程列表和待选目标。")
    return grid_id, button_selector


def read_config():
    with Path("config.json").open(encoding="utf-8") as config_file:
        return json.load(config_file)


def read_courses(path):
    with Path(path).open(encoding="utf-8") as course_file:
        data = json.load(course_file)
    keywords = []
    for course in data.get("courses", []):
        if course.get("enabled", True):
            keywords.extend(course.get("keywords", []))
    if not keywords:
        raise ValueError("courses.json 中没有启用的课程关键词")
    return keywords


def build_driver():
    options = Options()
    # 使用项目目录下的独立配置，避免占用用户正在使用的 Chrome 配置。
    profile_dir = Path.cwd() / f".chrome-profile-{datetime.now():%Y%m%d-%H%M%S-%f}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--disable-gpu")
    # 优先使用 webdriver_manager 已下载的本地驱动，避免受限环境下再次联网下载。
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


def _print_page_snippet(driver, label):
    log(f"[{label}] 当前URL：{driver.current_url}")
    log(f"[{label}] 页面标题：{driver.title}")
    for attribute in ["innerHTML", "textContent"]:
        try:
            body = driver.find_element(By.TAG_NAME, "body").get_attribute(attribute)
            if body:
                snippet = " ".join(str(body).split())[:1200]
                log(f"[{label}] body.{attribute} 片段：{snippet}")
                break
        except NoSuchElementException:
            continue


_USE_LOGIN_HELPER = False
_LOGIN_MAX_ATTEMPTS = 6


def _manual_login(driver, config, timeout):
    driver.get(config["url"])
    wait = WebDriverWait(driver, timeout)

    user_input = wait.until(ec.element_to_be_clickable((By.ID, "loginName")))
    password_input = wait.until(ec.element_to_be_clickable((By.ID, "loginPwd")))
    user_input.clear()
    user_input.send_keys(config["UserId"])
    password_input.clear()
    password_input.send_keys(config["PassWd"])

    log("已填写账号密码，请在浏览器中完成验证码并手动点击登录。")
    log("等待登录成功：不会仅根据 index_nju.html 判断，检测到首页菜单后才跳转。")

    def login_state(current_driver):
        visible_login_fields = [
            element
            for element in current_driver.find_elements(By.ID, "loginName")
            + current_driver.find_elements(By.ID, "loginPwd")
            if element.is_displayed()
        ]
        body_text = " ".join(
            current_driver.find_element(By.TAG_NAME, "body").text.split()
        )
        text_markers = ("我的选课", "退出登录", "安全退出", "注销", "个人信息")
        matched = next((marker for marker in text_markers if marker in body_text), None)
        structural_markers = {
            "course_link": len(current_driver.find_elements(By.CSS_SELECTOR, "a[href*='course_nju']")),
            "course_container": len(current_driver.find_elements(By.ID, "xkTabContainer")),
        }
        has_home_structure = bool(matched or any(structural_markers.values()))
        return (
            len(visible_login_fields),
            matched,
            has_home_structure,
            structural_markers,
            current_driver.current_url,
            body_text[:180],
        )

    stable_success_count = 0
    end_time = time.monotonic() + timeout
    while time.monotonic() < end_time:
        try:
            (
                field_count,
                marker,
                has_home_structure,
                structural_markers,
                current_url,
                body_preview,
            ) = login_state(driver)
        except (NoSuchElementException, TimeoutException):
            field_count, marker, has_home_structure = -1, None, False
            structural_markers, current_url, body_preview = {}, driver.current_url, ""
        log(
            f"[登录检测] url={current_url} 可见登录框={field_count} "
            f"首页文字特征={marker or '未发现'} 首页结构={has_home_structure} "
            f"结构={structural_markers} 页面={body_preview!r}"
        )
        if field_count == 0 and has_home_structure:
            stable_success_count += 1
            if stable_success_count >= 2:
                break
        else:
            stable_success_count = 0
        time.sleep(2)
    else:
        raise TimeoutException("等待登录成功超时：仍未检测到登录后的首页菜单。")

    base_url = driver.current_url.split("/sys/")[0]
    course_url = base_url + "/sys/xsxkapp/course_nju.html"
    driver.get(course_url)
    wait.until(lambda current_driver: "course_nju.html" in current_driver.current_url)
    log("已进入课程页面，开始检测课程。")


def login(driver, config, timeout):
    """登录入口：默认走原有人工登录；启用 --use-login-helper 时改用 login_helper 模块。"""
    if _USE_LOGIN_HELPER:
        from login_helper import perform_login
        perform_login(driver, config, timeout=timeout, max_attempts=_LOGIN_MAX_ATTEMPTS)
        wait = WebDriverWait(driver, timeout)
        base_url = driver.current_url.split("/sys/")[0]
        course_url = base_url + "/sys/xsxkapp/course_nju.html"
        driver.get(course_url)
        wait.until(lambda current_driver: "course_nju.html" in current_driver.current_url)
        log("已进入课程页面，开始检测课程。")
        return
    _manual_login(driver, config, timeout)



def click_refresh(driver):
    buttons = driver.find_elements(
        By.XPATH,
        "//button[contains(normalize-space(.), '刷新')] | "
        "//a[contains(normalize-space(.), '刷新')] | "
        "//*[(@title='刷新' or @aria-label='刷新')]",
    )
    for button in buttons:
        try:
            if button.is_displayed() and button.is_enabled():
                driver.execute_script("arguments[0].click();", button)
                return True
        except StaleElementReferenceException:
            # 页面正在重绘导致元素失效，跳过该按钮，交给刷新/重试路径处理。
            continue
    return False


def open_plan_courses(driver, timeout):
    wait = WebDriverWait(driver, timeout)
    tabs = wait.until(
        ec.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "#xkTabContainer [cv-role='tab']")
        )
    )
    tab = next(
        (item for item in tabs if item.is_displayed() and item.get_attribute("role-val") == "2"),
        None,
    )
    grid_id = "fankcGrid"
    button_selector = "#fankcGrid a[role='xk']"
    if tab is None:
        tab = next(
            (item for item in tabs if item.is_displayed() and "本轮次开放" in item.text),
            None,
        )
        grid_id = "blcAllCourseGrid"
        button_selector = "#blcAllCourseGrid [role='xkzgjy']"
    if tab is None:
        raise RuntimeError("未找到可见的方案课程或本轮次开放课程标签。")
    driver.execute_script("arguments[0].click();", tab)
    wait.until(ec.presence_of_element_located((By.ID, grid_id)))

    plan_filter = driver.find_elements(By.ID, "blc_query_jxsfankc")
    if plan_filter and plan_filter[0].is_displayed():
        driver.execute_script(
            "arguments[0].value = '1'; arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            plan_filter[0],
        )
        query = wait.until(
            ec.element_to_be_clickable(
                (By.CSS_SELECTOR, "#blcAllQueryForm [role-action='query']")
            )
        )
        driver.execute_script("arguments[0].click();", query)
    wait.until(ec.presence_of_element_located((By.CSS_SELECTOR, f"#{grid_id}")))
    return grid_id, button_selector


def find_matching_courses(driver, keywords, button_selector):
    results = []
    for button in driver.find_elements(By.CSS_SELECTOR, button_selector):
        try:
            if not button.is_displayed() or not button.is_enabled():
                continue
            row = button.find_element(By.XPATH, "ancestor::tr[1]")
            text = " ".join(row.text.split())
        except StaleElementReferenceException:
            # 页面正在重绘导致元素失效，跳过该行。
            continue
        if keywords and not any(keyword in text for keyword in keywords):
            continue
        available = not any(marker in text for marker in UNAVAILABLE_MARKERS)
        results.append((text, button, available))
    return results


def test_click_first_matching_course(driver, keywords, button_selector, timeout):
    """Ignore availability text and click the first keyword-matching button once."""
    if not click_refresh(driver):
        log("[点击测试] 页面没有找到刷新按钮，直接等待选课列表加载。")
    else:
        log("[点击测试] 已触发一次刷新，等待选课列表加载。")
    WebDriverWait(driver, timeout).until(
        ec.presence_of_all_elements_located((By.CSS_SELECTOR, button_selector))
    )
    candidates = []
    for button in driver.find_elements(By.CSS_SELECTOR, button_selector):
        try:
            row = button.find_element(By.XPATH, "ancestor::tr[1]")
        except NoSuchElementException:
            continue
        text = " ".join(row.text.split())
        if keywords and not any(keyword in text for keyword in keywords):
            continue
        candidates.append((text, button))

    if not candidates:
        log("[点击测试] 页面已加载选课按钮，但没有找到关键词对应课程。")
        log(f"[点击测试] 当前按钮选择器：{button_selector}")
        raise RuntimeError("点击测试失败：没有找到关键词对应的选课按钮。")

    text, button = candidates[0]
    log(f"[点击测试] 找到关键词课程：{text[:180]}")
    before_text = text
    log(
        "[点击测试] 按钮状态："
        f" displayed={button.is_displayed()} enabled={button.is_enabled()}"
        f" disabled={button.get_attribute('disabled')!r}"
        f" aria-disabled={button.get_attribute('aria-disabled')!r}"
        f" class={button.get_attribute('class')!r}"
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
    driver.execute_script("arguments[0].click();", button)
    log("[点击测试] 已执行一次选课按钮点击，等待确认弹窗。")
    confirmed, dialog_messages = click_confirm_dialogs(driver, timeout)
    for index, message in enumerate(dialog_messages, start=1):
        log(f"[点击测试] 弹窗 {index} 内容：{message[:500]}")
    print_feedback(driver, "点击测试")
    log(f"[点击测试] 点击前课程行：{before_text[:300]}")
    log(f"[点击测试] 已处理确认弹窗 {confirmed} 个。测试结束。")


def print_feedback(driver, label="选课"):
    """Print visible site feedback and classify the latest course-selection result."""
    texts = []
    selectors = (
        ".zeromodal, .zeromodal-dialog, .modal, .modal-dialog, "
        ".alert, .toast, .notification, [role='alert'], [role='dialog']"
    )
    for element in driver.find_elements(By.CSS_SELECTOR, selectors):
        if element.is_displayed():
            value = " ".join(element.text.split())
            if value and value not in texts:
                texts.append(value)
    if texts:
        for text in texts:
            log(f"[{label}反馈] {text[:500]}")
    else:
        log(f"[{label}反馈] 未找到可见弹窗或提示框。")

    feedback_text = " ".join(" ".join(texts).split())
    result_groups = {
        "成功": ("选课成功", "选择成功", "已选课程", "已选"),
        "失败-已满": ("已满", "满额", "无余量", "人数已满"),
        "失败-冲突": ("冲突", "时间冲突", "课程冲突"),
        "失败-条件": ("不满足", "不能选", "不可选", "不允许"),
        "失败": ("选课失败", "选择失败", "失败"),
    }
    matched = [
        group
        for group, markers in result_groups.items()
        if any(marker in feedback_text for marker in markers)
    ]
    log(f"[{label}结果] {', '.join(matched) if matched else '暂未识别明确结果'}")
    return matched, feedback_text


def click_confirm_dialogs(driver, timeout):
    wait = WebDriverWait(driver, timeout)
    confirm_xpath = (
        "//*[self::button or self::a or @role='button']["
        "contains(normalize-space(.), '确定') or "
        "contains(normalize-space(.), '确认')]"
    )
    clicked = 0
    dialog_messages = []
    for _ in range(2):
        try:
            confirm = WebDriverWait(driver, timeout if clicked == 0 else 3).until(
                lambda current_driver: next(
                    (
                        element
                        for element in current_driver.find_elements(By.XPATH, confirm_xpath)
                        if element.is_displayed() and element.is_enabled()
                    ),
                    None,
                )
            )
        except TimeoutException:
            break
        dialog_ancestors = confirm.find_elements(
            By.XPATH,
            "ancestor::*[contains(@class, 'modal') or contains(@class, 'dialog')][1]",
        )
        dialog_text = (
            " ".join(dialog_ancestors[0].text.split())
            if dialog_ancestors
            else " ".join(confirm.find_element(By.XPATH, "..//..").text.split())
        )
        dialog_messages.append(dialog_text)
        log(f"[确认弹窗] 点击前内容：{dialog_text[:500]}")
        driver.execute_script("arguments[0].click();", confirm)
        clicked += 1
        time.sleep(0.6)
    return clicked, dialog_messages


def main():
    parser = argparse.ArgumentParser(description="持续检测方案内课程并自动补选")
    parser.add_argument("--courses", default="courses.json", help="课程配置文件路径")
    parser.add_argument("--min-interval", type=float, default=60, help="最短刷新间隔，单位秒")
    parser.add_argument("--max-interval", type=float, default=300, help="最长刷新间隔，单位秒")
    parser.add_argument("--timeout", type=float, default=30, help="页面元素等待时间")
    parser.add_argument("--dry-run", action="store_true", help="只检测，不点击选课和确定")
    parser.add_argument("--test-click", action="store_true", help="忽略已满状态，仅点击第一条匹配课程一次后退出")
    parser.add_argument("--missing-rounds", type=int, default=5, help="连续多少轮找不到待选关键词后退出")
    parser.add_argument("--use-login-helper", action="store_true", help="使用 login_helper + OCR 全自动登录（默认关闭）")
    parser.add_argument("--login-max-attempts", type=int, default=6, help="login_helper 自动登录最大尝试次数")
    args = parser.parse_args()
    global _USE_LOGIN_HELPER, _LOGIN_MAX_ATTEMPTS
    _USE_LOGIN_HELPER = args.use_login_helper
    _LOGIN_MAX_ATTEMPTS = args.login_max_attempts

    if (
        args.min_interval <= 0
        or args.max_interval < args.min_interval
        or args.timeout <= 0
        or args.missing_rounds <= 0
        or args.login_max_attempts <= 0
    ):
        parser.error("参数必须满足 0 < min-interval <= max-interval、timeout > 0、missing-rounds > 0、login-max-attempts > 0")

    log_path = start_logging()
    log(f"日志文件：{log_path.resolve()}")
    config = read_config()
    keywords = read_courses(args.courses)
    state = load_state(keywords)
    pending_keywords = state["pending_keywords"]
    log(f"已启用课程关键词：{', '.join(pending_keywords)}")
    if state["successful_courses"] or state["refresh_count"]:
        log(
            f"[状态恢复] 已成功课程={len(state['successful_courses'])}，"
            f"待选关键词={', '.join(pending_keywords) or '无'}，"
            f"累计刷新={state['refresh_count']}，上次运行={state['last_run_at'] or '无'}。"
        )
    driver = build_driver()
    try:
        login(driver, config, args.timeout)
        grid_id, button_selector = open_plan_courses(driver, args.timeout)
        if args.test_click:
            test_click_first_matching_course(driver, pending_keywords, button_selector, args.timeout)
            return
        missing_rounds = 0
        refresh_count = state["refresh_count"]
        last_refresh_at = time.monotonic()
        success_count = 0
        failure_count = 0
        consecutive_recoveries = 0

        def do_recover(reason):
            nonlocal grid_id, button_selector, consecutive_recoveries
            consecutive_recoveries += 1
            if consecutive_recoveries > MAX_CONSECUTIVE_RECOVERIES:
                log(
                    f"[会话] 连续 {MAX_CONSECUTIVE_RECOVERIES} 次自动恢复失败，"
                    "停止自动恢复并退出。"
                )
                raise RuntimeError(
                    f"连续 {MAX_CONSECUTIVE_RECOVERIES} 次自动恢复登录失败"
                )
            log(f"[会话] 自动恢复（第 {consecutive_recoveries} 次）：{reason}")
            grid_id, button_selector = recover_session(
                driver, config, args.timeout, state
            )
            consecutive_recoveries = 0
            log("[会话] 自动恢复完成，继续监控。")

        while True:
            try:
                handle_possible_alert(driver)
                if not pending_keywords:
                    log(
                        f"[任务完成] 所有目标课程均已确认选课成功，结束循环。"
                        f" 刷新次数={refresh_count} 成功={success_count} 失败={failure_count}"
                    )
                    return
                if session_expired(driver):
                    do_recover("监测点发现登录已失效")
                    continue
                refresh_count += 1
                state["refresh_count"] = refresh_count
                save_state(state)
                refresh_started_at = time.monotonic()
                elapsed_since_refresh = refresh_started_at - last_refresh_at
                log(
                    f"[刷新] 第 {refresh_count} 次刷新开始；"
                    f"距离上一轮刷新 {elapsed_since_refresh:.1f} 秒。"
                )
                if not click_refresh(driver):
                    if session_expired(driver):
                        do_recover("刷新前发现登录已失效")
                        continue
                    driver.refresh()
                    if session_expired(driver):
                        do_recover("刷新后立即发现登录已失效")
                        continue
                    try:
                        grid_id, button_selector = open_plan_courses(driver, args.timeout)
                    except TimeoutException:
                        log("[会话] 刷新后课程列表未正常出现（可能已掉线），尝试自动恢复登录。")
                        do_recover("刷新后课程列表超时未出现")
                        continue
                time.sleep(0.8)
                last_refresh_at = time.monotonic()
                if session_expired(driver):
                    do_recover("刷新后监测点发现登录已失效")
                    continue
                matches = find_matching_courses(driver, pending_keywords, button_selector)
                log(
                    f"[课程匹配] 本轮找到 {len(matches)} 条待选关键词课程；"
                    f"剩余目标：{', '.join(pending_keywords)}。"
                )
                if not matches:
                    missing_rounds += 1
                    log(
                        f"[任务状态] 连续 {missing_rounds}/{args.missing_rounds} 轮"
                        "没有找到剩余目标课程。"
                    )
                    if missing_rounds >= args.missing_rounds:
                        log(f"[任务结束] 长时间未找到目标关键词：{', '.join(pending_keywords)}。")
                        return
                else:
                    missing_rounds = 0
                for index, (text, _, is_available) in enumerate(matches, start=1):
                    status = "有余量" if is_available else "已满/不可选"
                    log(f"[课程匹配 {index}] 已找到 [{status}]：{text[:180]}")
                    if not is_available:
                        log(f"[选课动作 {index}] 跳过点击：课程已满或不可选。")
                available = [(text, button) for text, button, is_available in matches if is_available]
                if not available:
                    if matches:
                        log("[选课结果] 找到了关键词课程，但当前没有可选课程，继续刷新。")
                    else:
                        log("[选课结果] 本轮没有找到关键词对应课程，继续刷新。")
                    delay = random.uniform(args.min_interval, args.max_interval)
                    log(f"[等待] 下一轮刷新将在 {delay:.1f} 秒后开始。")
                    time.sleep(delay)
                    continue

                for text, select_button in available:
                    log(f"[选课动作] 找到有余量课程，准备点击选课按钮：{text[:180]}")
                    if args.dry_run:
                        log("[选课结果] 当前为 --dry-run，已跳过实际点击。")
                        continue

                    driver.execute_script("arguments[0].click();", select_button)
                    log("[选课动作] 已点击选课按钮，等待确认弹窗。")
                    confirmed, _ = click_confirm_dialogs(driver, args.timeout)
                    result, feedback_text = print_feedback(driver)
                    if "成功" in result:
                        success_count += 1
                        completed = [keyword for keyword in pending_keywords if keyword in text]
                        pending_keywords = [
                            keyword for keyword in pending_keywords if keyword not in completed
                        ]
                        state["successful_courses"].append(text)
                        state["pending_keywords"] = pending_keywords
                        state["last_failure_reason"] = ""
                        save_state(state)
                        log(
                            f"[选课成功] 已完成目标：{', '.join(completed) or text[:100]}；"
                            f"剩余目标：{', '.join(pending_keywords) or '无'}。"
                        )
                    else:
                        failure_count += 1
                        state["last_failure_reason"] = feedback_text[:500] or "网站未返回明确失败原因"
                        state["pending_keywords"] = pending_keywords
                        save_state(state)
                        log(
                            f"[选课失败/待重试] 本次未确认成功，保留目标课程；"
                            f"已处理确认弹窗 {confirmed} 个。"
                        )
                    time.sleep(1)
                delay = random.uniform(args.min_interval, args.max_interval)
                log(f"[等待] 下一轮刷新将在 {delay:.1f} 秒后开始。")
                time.sleep(delay)
            except Exception as error:
                log(f"[异常] 监控循环出现未处理异常：{error!r}")
                log("[异常] traceback: " + traceback.format_exc().replace("\n", " | "))
                alert_text = handle_possible_alert(driver)
                if alert_text:
                    log(f"[异常] 捕获到 JS 弹窗内容：{alert_text[:200]}")
                expired = session_expired(driver) or any(
                    marker in alert_text for marker in LOGIN_EXPIRED_MARKERS
                )
                if not expired:
                    log("[会话] 异常后未确认登录失效，退出监控循环。")
                    try:
                        _print_page_snippet(driver, "异常页")
                    except Exception as snippet_error:
                        log(f"[异常] 页面信息采集失败：{snippet_error!r}")
                    raise
                recovered = False
                while consecutive_recoveries < MAX_CONSECUTIVE_RECOVERIES:
                    log(
                        "[会话] 异常后页面呈未登录状态，进入自动恢复"
                        f"（第 {consecutive_recoveries + 1} 次）。"
                    )
                    try:
                        do_recover("异常安全网：页面呈未登录状态")
                    except Exception as recover_error:
                        log(f"[会话] 自动恢复失败：{recover_error!r}")
                        log(
                            "[会话] 恢复失败 traceback: "
                            + traceback.format_exc().replace("\n", " | ")
                        )
                        continue
                    recovered = True
                    break
                if not recovered:
                    log(
                        f"[会话] 连续 {MAX_CONSECUTIVE_RECOVERIES} 次自动恢复失败，"
                        "退出监控循环。"
                    )
                    try:
                        _print_page_snippet(driver, "异常页")
                    except Exception as snippet_error:
                        log(f"[异常] 页面信息采集失败：{snippet_error!r}")
                    raise RuntimeError(
                        f"连续 {MAX_CONSECUTIVE_RECOVERIES} 次自动恢复登录失败"
                    )
                continue

    except (TimeoutException, NoSuchElementException) as error:
        log(f"页面元素等待失败：{error}")
        log("traceback: " + traceback.format_exc().replace("\n", " | "))
        try:
            log(f"当前 URL：{driver.current_url}")
            log(f"页面标题：{driver.title}")
            _print_page_snippet(driver, "异常页")
            tabs = driver.find_elements(By.CSS_SELECTOR, "#xkTabContainer [cv-role='tab']")
            log("可见标签：" + " | ".join(tab.text.strip() for tab in tabs if tab.is_displayed()))
        except Exception as diagnostic_error:
            log(f"[异常] 异常信息采集失败：{diagnostic_error!r}")
        raise SystemExit(1) from error
    except Exception as error:
        log(f"[异常] 程序意外结束：{error!r}")
        log("traceback: " + traceback.format_exc().replace("\n", " | "))
        try:
            _print_page_snippet(driver, "异常页")
        except Exception as diagnostic_error:
            log(f"[异常] 异常信息采集失败：{diagnostic_error!r}")
        raise SystemExit(1) from error
    finally:
        log("程序结束。日志已保存。按回车关闭浏览器...")
        input()
        driver.quit()
        close_logging()


if __name__ == "__main__":
    main()
