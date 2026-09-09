"""Monitor plan courses and submit course selections through the web UI."""

import argparse
import json
import random
import time
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait


UNAVAILABLE_MARKERS = ("已满", "满额", "无余量", "不可选")


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
    print(f"[{label}] 当前URL：{driver.current_url}")
    print(f"[{label}] 页面标题：{driver.title}")
    for attribute in ["innerHTML", "textContent"]:
        try:
            body = driver.find_element(By.TAG_NAME, "body").get_attribute(attribute)
            if body:
                snippet = " ".join(str(body).split())[:1200]
                print(f"[{label}] body.{attribute} 片段：{snippet}")
                break
        except NoSuchElementException:
            continue


def login(driver, config, timeout):
    driver.get(config["url"])
    wait = WebDriverWait(driver, timeout)

    user_input = wait.until(ec.element_to_be_clickable((By.ID, "loginName")))
    password_input = wait.until(ec.element_to_be_clickable((By.ID, "loginPwd")))
    user_input.clear()
    user_input.send_keys(config["UserId"])
    password_input.clear()
    password_input.send_keys(config["PassWd"])

    print("已填写账号密码，请在浏览器中完成验证码并手动点击登录。")
    print("等待登录成功：不会仅根据 index_nju.html 判断，检测到首页菜单后才跳转。")

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
        print(
            f"[登录检测] url={current_url} 可见登录框={field_count} "
            f"首页文字特征={marker or '未发现'} 首页结构={has_home_structure} "
            f"结构={structural_markers} 页面={body_preview!r}",
            flush=True,
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
    print("已进入课程页面，开始检测课程。")


def click_refresh(driver):
    buttons = driver.find_elements(
        By.XPATH,
        "//button[contains(normalize-space(.), '刷新')] | "
        "//a[contains(normalize-space(.), '刷新')] | "
        "//*[(@title='刷新' or @aria-label='刷新')]",
    )
    for button in buttons:
        if button.is_displayed() and button.is_enabled():
            driver.execute_script("arguments[0].click();", button)
            return True
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
        if not button.is_displayed() or not button.is_enabled():
            continue
        row = button.find_element(By.XPATH, "ancestor::tr[1]")
        text = " ".join(row.text.split())
        if keywords and not any(keyword in text for keyword in keywords):
            continue
        available = not any(marker in text for marker in UNAVAILABLE_MARKERS)
        results.append((text, button, available))
    return results


def test_click_first_matching_course(driver, keywords, button_selector, timeout):
    """Ignore availability text and click the first keyword-matching button once."""
    if not click_refresh(driver):
        print("[点击测试] 页面没有找到刷新按钮，直接等待选课列表加载。")
    else:
        print("[点击测试] 已触发一次刷新，等待选课列表加载。")
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
        print("[点击测试] 页面已加载选课按钮，但没有找到关键词对应课程。")
        print(f"[点击测试] 当前按钮选择器：{button_selector}")
        raise RuntimeError("点击测试失败：没有找到关键词对应的选课按钮。")

    text, button = candidates[0]
    print(f"[点击测试] 找到关键词课程：{text[:180]}")
    before_text = text
    print(
        "[点击测试] 按钮状态："
        f" displayed={button.is_displayed()} enabled={button.is_enabled()}"
        f" disabled={button.get_attribute('disabled')!r}"
        f" aria-disabled={button.get_attribute('aria-disabled')!r}"
        f" class={button.get_attribute('class')!r}"
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
    driver.execute_script("arguments[0].click();", button)
    print("[点击测试] 已执行一次选课按钮点击，等待确认弹窗。")
    confirmed, dialog_messages = click_confirm_dialogs(driver, timeout)
    for index, message in enumerate(dialog_messages, start=1):
        print(f"[点击测试] 弹窗 {index} 内容：{message[:500]}")
    print_feedback(driver, "点击测试")
    print(f"[点击测试] 点击前课程行：{before_text[:300]}")
    print(f"[点击测试] 已处理确认弹窗 {confirmed} 个。测试结束。")


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
            print(f"[{label}反馈] {text[:500]}")
    else:
        print(f"[{label}反馈] 未找到可见弹窗或提示框。")

    page_text = " ".join(driver.find_element(By.TAG_NAME, "body").text.split())
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
        if any(marker in page_text for marker in markers)
    ]
    print(f"[{label}结果] {', '.join(matched) if matched else '暂未识别明确结果'}")


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
        dialog_text = " ".join(confirm.find_element(By.XPATH, "ancestor::*[contains(@class, 'modal') or contains(@class, 'dialog')][1]").text.split()) if confirm.find_elements(By.XPATH, "ancestor::*[contains(@class, 'modal') or contains(@class, 'dialog')][1]") else " ".join(confirm.find_element(By.XPATH, "..//..").text.split())
        dialog_messages.append(dialog_text)
        print(f"[确认弹窗] 点击前内容：{dialog_text[:500]}")
        driver.execute_script("arguments[0].click();", confirm)
        clicked += 1
        time.sleep(0.6)
    return clicked, dialog_messages


def main():
    parser = argparse.ArgumentParser(description="持续检测方案内课程并自动补选")
    parser.add_argument("--courses", default="courses.json", help="课程配置文件路径")
    parser.add_argument("--min-interval", type=float, default=10, help="最短刷新间隔，单位秒")
    parser.add_argument("--max-interval", type=float, default=30, help="最长刷新间隔，单位秒")
    parser.add_argument("--timeout", type=float, default=30, help="页面元素等待时间")
    parser.add_argument("--dry-run", action="store_true", help="只检测，不点击选课和确定")
    parser.add_argument("--test-click", action="store_true", help="忽略已满状态，仅点击第一条匹配课程一次后退出")
    args = parser.parse_args()

    if args.min_interval <= 0 or args.max_interval < args.min_interval or args.timeout <= 0:
        parser.error("刷新间隔必须满足 0 < min-interval <= max-interval")

    config = read_config()
    keywords = read_courses(args.courses)
    print(f"已启用课程关键词：{', '.join(keywords)}")
    driver = build_driver()
    try:
        login(driver, config, args.timeout)
        grid_id, button_selector = open_plan_courses(driver, args.timeout)
        if args.test_click:
            test_click_first_matching_course(driver, keywords, button_selector, args.timeout)
            return
        while True:
            if not click_refresh(driver):
                driver.refresh()
                grid_id, button_selector = open_plan_courses(driver, args.timeout)
            time.sleep(0.8)
            matches = find_matching_courses(driver, keywords, button_selector)
            print(f"[课程匹配] 本轮找到 {len(matches)} 条关键词匹配课程。")
            for index, (text, _, is_available) in enumerate(matches, start=1):
                status = "有余量" if is_available else "已满/不可选"
                print(f"[课程匹配 {index}] 已找到 [{status}]：{text[:180]}")
                if not is_available:
                    print(f"[选课动作 {index}] 跳过点击：课程已满或不可选。")
            available = [(text, button) for text, button, is_available in matches if is_available]
            if not available:
                if matches:
                    print("[选课结果] 找到了关键词课程，但当前没有可选课程，继续刷新。")
                else:
                    print("[选课结果] 本轮没有找到关键词对应课程，继续刷新。")
                time.sleep(random.uniform(args.min_interval, args.max_interval))
                continue

            for text, select_button in available:
                print(f"[选课动作] 找到有余量课程，准备点击选课按钮：{text[:180]}")
                if args.dry_run:
                    print("[选课结果] 当前为 --dry-run，已跳过实际点击。")
                    continue

                driver.execute_script("arguments[0].click();", select_button)
                print("[选课动作] 已点击选课按钮，等待确认弹窗。")
                confirmed, _ = click_confirm_dialogs(driver, args.timeout)
                print_feedback(driver)
                print(f"[选课结果] 已处理确认弹窗 {confirmed} 个，继续检测其他课程。")
                time.sleep(1)
            time.sleep(random.uniform(args.min_interval, args.max_interval))
    except (TimeoutException, NoSuchElementException) as error:
        print(f"页面元素等待失败：{error}")
        print(f"当前 URL：{driver.current_url}")
        print(f"页面标题：{driver.title}")
        _print_page_snippet(driver, "异常页")
        tabs = driver.find_elements(By.CSS_SELECTOR, "#xkTabContainer [cv-role='tab']")
        print("可见标签：" + " | ".join(tab.text.strip() for tab in tabs if tab.is_displayed()))
        raise SystemExit(1) from error
    finally:
        input("按回车关闭浏览器...")
        driver.quit()


if __name__ == "__main__":
    main()
