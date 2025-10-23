import warnings

# RuntimeWarning: coroutine '...' was never awaited 경고를 무시하도록 설정 (경고 제거)
warnings.filterwarnings(
    "ignore",
    message="coroutine '.*' was never awaited",
    category=RuntimeWarning
)

import streamlit as st
import datetime
import threading
import time
import queue
import sys
import traceback
import requests
import ujson as json
import urllib3
import re
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed

# InsecureRequestWarning 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 🚨 KST 시간대 객체 전역 정의
KST = pytz.timezone('Asia/Seoul')


# ============================================================
# Utility Functions
# ============================================================

def log_message(message, message_queue):
    """
    백그라운드 스레드에서 호출. 메시지를 Queue에 넣습니다.
    🚨 로그 시간을 KST(한국 시간)로 표시합니다.
    """
    try:
        # 🚨 KST 기준의 현재 시간을 사용하여 로그에 표시
        now_kst = datetime.datetime.now(KST)
        timestamp = now_kst.strftime('%H:%M:%S.%f')[:-3]

        # 🚨 UI_LOG 접두사를 사용하여 메인 스레드에서 UI 업데이트에 사용됩니다.
        message_queue.put(f"🚨UI_LOG:[{timestamp}] {message}")
    except Exception:
        pass


def get_default_date(days):
    """오늘 날짜로부터 지정된 일수만큼 지난 날짜를 반환 (datetime.date 객체)"""
    # 🚨 KST 기준의 오늘 날짜를 기준으로 계산
    return (datetime.datetime.now(KST).date() + datetime.timedelta(days=days))


def format_time_for_api(time_str):
    """'HH:MM' 형태를 API에 맞는 'HHMM' 형태로 변환"""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{3,4}$', time_str) and time_str.isdigit():
        if len(time_str) == 4:
            return time_str
        elif len(time_str) == 3:
            return f"0{time_str}"
    return "0000"


def format_time_for_display(time_str):
    """'HHMM' 형태를 'HH:MM' 형태로 변환"""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{4}$', time_str) and time_str.isdigit():
        return f"{time_str[:2]}:{time_str[2:]}"
    return time_str


def wait_until(target_dt_kst, stop_event, message_queue, log_prefix="프로그램 실행",
               log_countdown=False):  # 🚨 변수명 target_dt_kst로 변경 (KST-aware 객체 수신)
    """
    특정 시간까지 대기 (쓰레드 내에서 실행)
    """
    global KST  # KST 전역 객체 사용

    # 🚨 로그 메시지에 KST 기준 명시
    log_message(f"⏳ {log_prefix} 대기중: {target_dt_kst.strftime('%H:%M:%S.%f')[:-3]} (KST 기준)", message_queue)
    last_remaining_sec = None
    # 🚨 카운트다운 시작 시점을 30초 전으로 고정합니다.
    log_remaining_start = 30
    while not stop_event.is_set():
        # 🚨 [KST 정확성] KST 기준으로 현재 시간을 가져와서 비교합니다.
        now_kst = datetime.datetime.now(KST)
        remaining_seconds = (target_dt_kst - now_kst).total_seconds()

        # 🚨 목표 시간이 1ms 이하로 남았거나 지났다면 즉시 탈출
        if remaining_seconds <= 0.001:
            break

        current_remaining_sec = int(remaining_seconds)
        if log_countdown and remaining_seconds <= log_remaining_start:
            if current_remaining_sec > 0 and current_remaining_sec != last_remaining_sec:
                # 🚨 [로그 수정] 사용자 요청 형식 반영
                log_message(f"⏳ 예약 시작 대기중 ({current_remaining_sec}초)", message_queue)
                last_remaining_sec = current_remaining_sec
        # 🚨 목표 시간에 가까워질수록 더 정밀하게 대기
        if remaining_seconds < 0.1:  # 0.1초 미만으로 남았을 때
            time.sleep(0.001)  # 1ms 단위로 정밀하게 대기
        elif remaining_seconds < 1:
            time.sleep(0.005)  # 5ms 단위로 대기
        else:
            # 🚨 Streamlit UI 갱신을 위해 메인 스레드에 제어권을 주기 위한 짧은 sleep 유지
            time.sleep(0.1)

    if not stop_event.is_set():
        # 🚨 실제 종료 시각과 목표 시각의 차이를 로그에 추가하여 정확도 확인
        actual_diff = (datetime.datetime.now(KST) - target_dt_kst).total_seconds()
        log_message(f"✅ 목표 시간 도달! {log_prefix} 스레드 즉시 실행. (종료 시각 차이: {actual_diff:.3f}초)", message_queue)


# ============================================================
# API Booking Core Class
# ============================================================
class APIBookingCore:
    def __init__(self, log_func, message_queue, stop_event):
        self.log_message_func = log_func
        self.message_queue = message_queue
        self.stop_event = stop_event
        self.session = requests.Session()
        self.course_detail_mapping = {
            "A": "참피온OUT", "B": "참피온IN", "C": "마스타OUT", "D": "마스타IN"
        }
        self.ms_num = ""
        self.ms_num_lock = threading.Lock()
        self.proxies = None

        # 🚨 KST 시간대 객체 정의 및 즉시 실행 플래그 추가
        self.KST = pytz.timezone('Asia/Seoul')
        self.force_immediate_start = False  # 초기값은 False

    def log_message(self, msg):
        self.log_message_func(msg, self.message_queue)

    def requests_login(self, usrid, usrpass, max_retries=3):
        """
        순수 requests를 이용한 API 로그인 시도 및 응답 텍스트에서 msNum을 직접 추출합니다.
        (msNum 추출 로직은 사용자 요청에 따라 기존 Streamlit 로직을 유지함)
        """
        login_url = "https://www.gakorea.com/controller/MemberController.asp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Referer": "https://www.gakorea.com/mobile/join/login.asp",  # 모바일 로그인 Referer
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"
        }
        payload = {
            "method": "doLogin", "coDiv": "undefined", "id": usrid, "pw": usrpass
        }

        for attempt in range(max_retries):
            if self.stop_event.is_set(): return {'result': 'fail', 'cookies': {}}
            try:
                self.log_message(f"🔄 API 로그인 및 msNum 추출 시도 중... (시도 {attempt + 1}/{max_retries})")

                res = self.session.post(login_url, headers=headers, data=payload, timeout=10, verify=False,
                                        allow_redirects=True)

                res.raise_for_status()
                cookies = dict(self.session.cookies)

                # 로그인 응답(res.text)에서 msNum을 직접 찾습니다.
                match = re.search(
                    r'(?:msNum|ms_num)\s*[:=]\s*["\']?(\d{10,})["\']?',
                    res.text,
                    re.IGNORECASE | re.DOTALL
                )

                if match:
                    # msNum을 찾으면 즉시 저장하고 성공 반환
                    with self.ms_num_lock:
                        self.ms_num = match.group(1)
                    self.log_message(f"✅ msNum 추출 성공 (로그인 응답): {self.ms_num}")
                    self.log_message("🔑 순수 API 로그인 완료. 세션 쿠키 및 msNum 확보.")
                    return {'result': 'success', 'cookies': cookies}

                # msNum을 못찾았지만, 세션 쿠키가 있거나 '로그아웃' 텍스트가 있다면
                if '로그아웃' in res.text or any('SESSIONID' in key for key in cookies):
                    self.log_message("❌ 로그인 세션은 성공했으나, 응답에서 msNum을 찾지 못했습니다.")
                    self.log_message(f"ℹ️ [진단용] 로그인 응답 HTML (일부): {res.text[:1000]}...")
                    return {'result': 'fail', 'cookies': {}}  # msNum 없으면 실패

                # 로그인 실패 메시지 감지
                elif '로그인 정보가 일치하지 않습니다' in res.text:
                    self.log_message("❌ 로그인 실패: 아이디 또는 비밀번호가 일치하지 않습니다.")
                    self.log_message("🚨UI_ERROR:로그인 실패: 아이디 또는 비밀번호를 확인해주세요.")
                    return {'result': 'fail', 'cookies': {}}

                self.log_message(f"❌ 로그인 실패 (msNum, 쿠키 모두 추출 실패). 응답: {res.text[:200]}")
                if attempt < max_retries - 1: time.sleep(0.1)

            except requests.RequestException as e:
                self.log_message(f"❌ 네트워크 오류: 로그인 중 오류 발생: {e}")
                if attempt < max_retries - 1: time.sleep(0.1)
            except Exception as e:
                self.log_message(f"❌ 예외 오류: 로그인 중 알 수 없는 오류 발생: {e}")
                break

        return {'result': 'fail', 'cookies': {}}

    def keep_session_alive(self, target_dt):
        """세션 유지를 위해 1분에 1회 서버에 접속 시도 (백그라운드 스레드에서 실행)"""
        # 🚨 target_dt는 KST datetime 객체입니다.

        self.log_message("✅ 세션 유지 스레드 시작. 1분마다 세션 유지를 시도합니다.")

        # 예약 시작 시간 전까지만 세션 유지 시도
        while not self.stop_event.is_set() and datetime.datetime.now(self.KST) < target_dt:

            # 1분에 1회 (60초)마다 세션 유지 시도
            time_to_sleep = 60.0

            try:
                # 로그인 페이지 GET 요청 (세션 유지 목적)
                self.session.get("https://www.gakorea.com/join/login.asp", timeout=5, verify=False,
                                 proxies=self.proxies)

                self.log_message("💚 [세션 유지] 세션 유지 요청 완료.")

            except Exception as e:
                self.log_message(f"❌ [세션 유지] 통신 오류 발생: {e}")

                # 다음 시도까지 대기
            i = 0
            # 🚨 KST 기준의 현재 시간이 target_dt를 넘지 않았는지 확인
            while i < time_to_sleep and not self.stop_event.is_set() and datetime.datetime.now(self.KST) < target_dt:
                time.sleep(1)  # 1초씩 짧게 쉬면서 중단 신호 확인
                i += 1

        if self.stop_event.is_set():
            self.log_message("🛑 세션 유지 스레드: 중단 신호 감지. 종료합니다.")
        else:
            self.log_message("✅ 세션 유지 스레드: 예약 정시 도달. 종료합니다.")

    # --------------------------------------------------------------------------
    # 🚨 예약 가능 신호 감지 로직 (PyQt 소스 로직 반영 - time.sleep(0) 유지)
    # --------------------------------------------------------------------------
    def wait_for_open_signal(self, target_date):
        """최대 7분 동안 '예약가능' 신호를 기다리는 감시 루프"""
        start_time = time.monotonic()
        MAX_WAIT_SECONDS = 420  # 최대 7분 (420초) 대기

        self.log_message(
            f"🚀 예약 오픈 감지 시작: {target_date}의 '예약가능' 버튼 생성을 기다립니다. (최대 {MAX_WAIT_SECONDS}초, 0ms 주기 확인)")

        while not self.stop_event.is_set():
            elapsed_time = time.monotonic() - start_time

            if elapsed_time > MAX_WAIT_SECONDS:
                self.log_message(f"❌ 예약 오픈 신호 감지에 실패했습니다. 최대 대기 시간({MAX_WAIT_SECONDS}초) 초과.")
                return False

            try:
                # check_booking_open_by_calendar 함수는 캘린더 API를 호출하여 예약 가능 여부를 확인합니다.
                if self.check_booking_open_by_calendar(target_date):
                    self.log_message(f"🎉 예약 오픈 감지 성공! (경과 시간: {elapsed_time:.3f}초) 예약 시도로 전환합니다.")
                    return True  # 성공적으로 예약 오픈 감지

                # time.sleep(0)로 CPU 양보 후 즉시 재시도
                time.sleep(0)
            except Exception:
                # 일시적인 오류(e.g. timeout)는 무시하고 즉시 재시도
                pass

        return False  # 중단 신호로 종료

    # --------------------------------------------------------------------------
    # 기존 check_booking_open_by_calendar 함수
    # --------------------------------------------------------------------------
    def check_booking_open_by_calendar(self, date):
        """해당일 '예약가능' 버튼 생성 여부 확인"""
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.gakorea.com",
            # 모바일 예약 페이지 Referer 사용
            "Referer": "https://www.gakorea.com/mobile/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }
        year_month = date[:6]

        if self.ms_num is None or self.ms_num == "":
            self.log_message("⚠️ check_booking_open_by_calendar: msNum 값이 설정되지 않아 API 호출에 실패할 수 있습니다.",
                             self.message_queue)

        payload = {
            "method": "getCalendar", "coDiv": "611", "selYm": year_month, "msNum": self.ms_num, "msDivision": "10",
            "msClass": "01", "msLevel": "00"
        }

        try:
            # 🚨 사용자 경험을 반영하여 timeout을 3.0초로 설정
            res = self.session.post(url, headers=headers, data=payload, timeout=3.0, verify=False)
            res.raise_for_status()
            data = json.loads(res.text)

            if 'rows' in data and data['rows']:
                for day_info in data['rows']:
                    if day_info.get('CL_SOLAR') == date:
                        # OPENDAY 필드에 '99999999'가 아니라면 예약 가능 신호
                        openday = day_info.get('OPENDAY', '99999999')
                        if openday != '99999999': return True
                return False
            return False
        except requests.RequestException:
            # 오류 발생 시 신호 감지 실패로 간주하고 루프에서 즉시 재시도
            return False

    def get_all_available_times(self, date):
        """모든 코스의 예약 가능 시간대 조회 (멀티스레딩 사용)"""
        self.log_message(f"⏳ {date} 모든 코스 예약 가능 시간대 조회 중... (멀티스레드)")
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.gakorea.com",
            # 모바일 예약 페이지 Referer 사용
            "Referer": "https://www.gakorea.com/mobile/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }
        cos_values = ["A", "B", "C", "D"]
        all_fetched_times = []

        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_cos = {executor.submit(self._fetch_tee_list, date, cos): cos for cos in cos_values}
            for future in as_completed(future_to_cos):
                times = future.result()
                if times: all_fetched_times.extend(times)

        unique_times_map = {}
        for t in all_fetched_times:
            # (시간, 코스, 파트)를 키로 사용하여 중복 제거
            time_key = (t[0], t[1], t[2])
            if time_key not in unique_times_map: unique_times_map[time_key] = t

        all_fetched_times = list(unique_times_map.values())
        self.log_message(f"✅ 총 {len(all_fetched_times)}개의 예약 가능 시간대 확보 완료.")
        return all_fetched_times

    def _fetch_tee_list(self, date, cos, max_retries=2):
        """단일 코스의 티 리스트 조회 (Thread Pool 내부에서 사용)"""
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.gakorea.com",
            "Referer": "https://www.gakorea.com/mobile/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }

        # --- msNum 확보 로직 ---
        if not self.ms_num:
            self.log_message("🛑 msNum 값이 없습니다. 로그인 단계에서 확보되지 않았습니다. API 호출 중단.")
            return []
        # ----------------------

        part = "1" if cos in ["A", "C"] else "2"
        payload = {
            "method": "getTeeList", "coDiv": "611", "date": date, "cos": cos, "part": part,
            "msNum": self.ms_num, "msDivision": "10", "msClass": "01", "msLevel": "00"
        }
        for attempt in range(max_retries):
            if self.stop_event.is_set(): return []
            try:
                res = self.session.post(url, headers=headers, data=payload, timeout=3.0, verify=False)
                res.raise_for_status()
                data = json.loads(res.text)
                times = [
                    (t['BK_TIME'], t['BK_COS'], t['BK_PART'], self.course_detail_mapping.get(cos, 'Unknown'), "611")
                    for t in data.get('rows', [])]
                course_type = "OUT" if part == "1" else "IN"
                self.log_message(f"🔍 getTeeList 완료 (cos={cos} {course_type}): {len(times)}개 시간대")
                return times
            except requests.RequestException as e:
                self.log_message(f"❌ getTeeList 실패 (cos={cos}, 시도 {attempt + 1}/{max_retries}): {e}")
                time.sleep(0.3)
        return []

    def filter_and_sort_times(self, all_times, start_time_str, end_time_str, target_courses, is_reverse):
        """시간대 필터링 및 정렬"""
        start_time_entry = format_time_for_api(start_time_str)
        end_time_entry = format_time_for_api(end_time_str)

        course_map = {
            "All": ["A", "B", "C", "D"],
            "참피온": ["A", "B"],
            "마스타": ["C", "D"]
        }
        target_course_codes = course_map.get(target_courses, [])

        filtered_times = []
        for t in all_times:
            time_val = format_time_for_api(t[0])
            cos_val = t[1]
            if start_time_entry <= time_val <= end_time_entry and cos_val in target_course_codes:
                filtered_times.append(t)

        filtered_times.sort(key=lambda x: int(format_time_for_api(x[0])), reverse=is_reverse)

        formatted_times = [f"{format_time_for_display(t[0])} ({self.course_detail_mapping.get(t[1])})" for t in
                           filtered_times]
        self.log_message(f"🔍 필터링/정렬 완료 (순서: {'역순' if is_reverse else '순차'}) - {len(filtered_times)}개 발견")

        # --- 상위 5개 시간대 전체 표시 ---
        if formatted_times:
            top_5 = formatted_times[:5]
            self.log_message("📜 **[최종 예약 우선순위 5개]**")
            for i, time_str in enumerate(top_5):
                self.log_message(f"   {i + 1}순위: {time_str}")
        # ----------------------------------------------------

        return filtered_times

    def try_reservation(self, date, course, time_, bk_cos, bk_part, co_div, max_retries=3):
        """실제 예약 API 요청"""
        self.log_message(f"🎯 {course} 코스 {time_} 예약 시도 중...")
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.gakorea.com",
            # 모바일 예약 페이지 Referer 사용
            "Referer": "https://www.gakorea.com/mobile/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }
        time_for_api = format_time_for_api(time_)
        # self.ms_num을 사용
        payload = {
            "method": "doReservation", "coDiv": co_div, "day": date, "cos": bk_cos, "time": time_for_api, "tCnt": "0",
            "msNum": self.ms_num, "msDivision": "10", "msClass": "01", "msLevel": "00",
            "media": "R", "gubun": "M", "_": str(int(time.time() * 1000))
        }

        for attempt in range(max_retries):
            if self.stop_event.is_set(): return False
            try:
                res = self.session.post(url, headers=headers, data=payload, timeout=5.0, verify=False)
                res.raise_for_status()
                data = json.loads(res.text)

                if data.get('resultCode') == '0000':
                    # 예약 성공 API 응답 수신
                    self.log_message(f"👍 예약 성공 API 응답 수신: {course} {time_} (시도 {attempt + 1}/{max_retries})")
                    return True
                else:
                    self.log_message(
                        f"⚠️ 예약 실패: {course} {time_}, 응답: {data.get('resultMsg', '알 수 없음')} (시도 {attempt + 1}/{max_retries})")

                if attempt < max_retries - 1: time.sleep(0.1)
            except requests.RequestException as e:
                self.log_message(f"❌ 네트워크 오류: {course} {time_}, 오류: {e} (시도 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1: time.sleep(0.1)
        return False

    def run_api_booking(self, date, target_course_name, test_mode, sorted_available_times, delay_seconds):
        """정렬된 시간 순서대로 예약 시도 실행"""
        # run_api_booking 내부에 있던 '[API EXEC] 🚀 API 예약 프로세스 즉시 시작!' 로그는 start_pre_process로 이동됨.

        if delay_seconds > 0:
            self.log_message(f"⏳ 설정된 예약 지연({delay_seconds}초)만큼 대기합니다...")
            time.sleep(delay_seconds)
            self.log_message("✅ 지연 시간 대기 완료. 예약을 시작합니다.")

        try:
            # 상위 5개 시간대만 시도
            times_to_attempt = sorted_available_times[:5]
            if not times_to_attempt:
                self.log_message("❌ 설정된 조건에 맞는 예약 가능 시간대가 없습니다. API 예약 중단.")
                return False

            self.log_message(f"🔎 {target_course_name} 코스에 대해 정렬된 시간 순서대로 (상위 {len(times_to_attempt)}개) 예약 시도...")

            if test_mode:
                first_time = times_to_attempt[0]
                formatted_time = f"{format_time_for_display(first_time[0])} ({self.course_detail_mapping.get(first_time[1])})"
                self.log_message(f"✅ 테스트 모드: 1순위 코스 예약 가능 시간대: {formatted_time} (실제 예약 시도는 건너뜀)")
                return True

            for i, time_info in enumerate(times_to_attempt):
                if self.stop_event.is_set(): return False

                time_, bk_cos, bk_part, course_nm, co_div = time_info
                formatted_time = format_time_for_display(time_)

                self.log_message(f"➡️ [시도 {i + 1}/{len(times_to_attempt)}] 예약 시도: {course_nm} {formatted_time}")
                success = self.try_reservation(date, course_nm, formatted_time, bk_cos, bk_part, co_div)

                if success:
                    self.log_message(f"🎉🎉🎉 예약 성공!!! [{i + 1}순위] {course_nm} {formatted_time} 시간 예약에 성공했습니다! 🎉🎉🎉")

                    # 🚨 사용자 요청에 따라 예약 성공 후 5초 대기 (서버 부하/지연 대응)
                    POST_SUCCESS_DELAY = 5
                    self.log_message(f"⏳ 서버 성공 신호 지연에 대비하여 **{POST_SUCCESS_DELAY}초** 대기...")
                    time.sleep(POST_SUCCESS_DELAY)
                    self.log_message("✅ 5초 대기 완료. 예약 프로세스를 종료합니다.")

                    return True

            self.log_message(f"❌ 상위 {len(times_to_attempt)}개 시도가 모두 실패했습니다.")
            return False

        except Exception as e:
            self.log_message(f"FATAL: API 예약 프로세스 중 에러 발생: {e}")
            raise


# ============================================================
# Main Threading Logic - start_pre_process (핵심 프로세스)
# ============================================================
def start_pre_process(message_queue, stop_event, inputs):
    """
    백그라운드 스레드에서 실행되는 예약 핵심 로직 (시간 제어 포함)
    """
    global KST
    log_message("[INFO] ⚙️ 예약 시작 조건 확인 완료.", message_queue)
    try:
        params = inputs
        core = APIBookingCore(log_message, message_queue, stop_event)

        # 1. 로그인
        log_message("✅ 작업 진행 중: API 로그인 및 msNum 확보 시도...", message_queue)
        login_result = core.requests_login(params.get('id'), params.get('pw'))
        if login_result['result'] != 'success':
            log_message("❌ 로그인 또는 msNum 확보 실패. 아이디/비밀번호를 확인하거나 네트워크를 점검하세요.", message_queue)
            message_queue.put(f"🚨UI_ERROR:로그인 또는 msNum 확보 실패: 아이디/비밀번호 또는 서버 응답을 확인하세요.")
            return
        log_message("✅ 로그인 및 msNum 확보 성공.", message_queue)

        # 2. 가동 시작 시간 (KST) 계산
        run_datetime_str = f"{params.get('run_date')} {params.get('run_time')}"
        run_datetime_naive = datetime.datetime.strptime(run_datetime_str, '%Y%m%d %H:%M:%S')
        # UI 입력 시간을 KST로 변환하여 시간대 정보 부여 (예약 정시)
        run_datetime_kst = KST.localize(run_datetime_naive)

        # 🚨 [수정] 2-A. 세션 유지를 위한 백그라운드 스레드 시작 (로그인 직후 ~ 예약 정시까지 작동)
        # run_datetime_kst(예약 정시)를 목표 종료 시간으로 keep_session_alive 함수에 전달합니다.
        session_thread = threading.Thread(
            target=core.keep_session_alive,
            args=(run_datetime_kst,),
            daemon=True
        )
        session_thread.start()
        log_message("✅ 세션 유지 스레드 백그라운드에서 시작됨 (1분마다 서버 접속 시도).", message_queue)

        # 3. 티 타임 조회 시작 시점 (KST 기준, 예약 정시 60초 전)
        pre_fetch_start_kst = run_datetime_kst - datetime.timedelta(seconds=60)

        # 4. 예약 시도 대기 시작 시점 (KST 기준, 예약 정시 30초 전)
        # booking_wait_start_kst = run_datetime_kst - datetime.timedelta(seconds=30) # 사용되지 않아 주석처리

        # 5. 티 타임 조회 (60초 전)까지 대기
        now_kst = datetime.datetime.now(KST)

        if now_kst < pre_fetch_start_kst:
            # 60초 전까지 대기
            log_message(f"⏳ 티 타임 조회 대기중. 목표 시각(KST): {pre_fetch_start_kst.strftime('%H:%M:%S')}", message_queue)

            # log_countdown=False로 설정하여 60초 전 대기에서는 카운트다운 로그를 표시하지 않습니다.
            # 🚨 KST-aware 객체를 직접 전달하도록 수정 (클라우드 환경 대응)
            wait_until(pre_fetch_start_kst, stop_event, message_queue, log_prefix="티 타임 조회")
        else:
            # 목표 시간이 이미 지났다면 즉시 실행
            log_message(
                f"✅ [즉시 실행 감지] 현재 KST 시간({now_kst.strftime('%H:%M:%S')})이 티 타임 조회 목표 시각({pre_fetch_start_kst.strftime('%H:%M:%S')})보다 늦어 즉시 조회됩니다.",
                message_queue)

        if stop_event.is_set(): return

        # 6. 티 타임 조회 및 필터링
        log_message("🔎 티 타임 조회 시작: 서버에서 예약 가능한 시간대를 가져옵니다.", message_queue)
        all_times = core.get_all_available_times(params.get('date'))

        is_reverse_order = params.get('order', '순방향 (오름)') == '역순 (내림)'

        log_message(
            f"🔎 필터링 조건: {params.get('start_time', '06:00')} ~ {params.get('end_time', '09:00')}, 코스: {params.get('course_type', 'All')}",
            message_queue)

        sorted_times = core.filter_and_sort_times(
            all_times,
            params.get('start_time', '06:00'),
            params.get('end_time', '09:00'),
            params.get('course_type', 'All'),
            is_reverse_order
        )

        # 7. 예약 시도 시간 (KST)까지 대기
        now_kst = datetime.datetime.now(KST)
        if now_kst < run_datetime_kst:

            # 🚨 예약 정시까지 대기하며, 30초 전부터는 카운트다운 로그를 표시합니다.
            log_message(f"⏳ 예약 시도 대기중. 목표 시각(KST): {run_datetime_kst.strftime('%H:%M:%S')}", message_queue)

            # 🚨 30초 전부터 카운트다운을 시작하도록 wait_until을 사용합니다.
            # 🚨 KST-aware 객체를 직접 전달하도록 수정 (클라우드 환경 대응)
            wait_until(run_datetime_kst, stop_event, message_queue, log_prefix="최종 예약 시도", log_countdown=True)

        else:
            log_message(
                f"✅ [지연 감지] 현재 KST 시간({now_kst.strftime('%H:%M:%S')})이 예약 정시({run_datetime_kst.strftime('%H:%M:%S')})보다 늦어 즉시 예약 시도됩니다.",
                message_queue)

        if stop_event.is_set(): return

        # 🚨 [핵심 수정 부분] 예약 정시 도달 후, '예약가능' 신호를 감지할 때까지 대기
        # 이 함수 내부에서 "🚀 예약 오픈 감지 시작" 로그가 출력됩니다.
        if not core.wait_for_open_signal(params.get('date')):
            # 감지 실패 시(7분 초과 또는 중단), 프로세스 종료
            message_queue.put(f"🚨UI_ERROR:예약 오픈 감지 실패: 예약 가능한 신호가 포착되지 않아 프로세스를 종료합니다.")
            return

        # 8. 예약 시도 (감지 성공 시에만 실행)
        # run_api_booking 내부에 있던 'API EXEC' 로그를 감지 성공 후로 이동합니다.
        log_message("[API EXEC] 🚀 API 예약 프로세스 즉시 시작!", message_queue)
        core.run_api_booking(
            date=params.get('date'),
            target_course_name=params.get('course_type', 'All'),
            test_mode=params.get('test_mode', True),
            sorted_available_times=sorted_times,
            delay_seconds=int(params.get('delay', 0))
        )

    except Exception:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback_details = traceback.format_exception(exc_type, exc_value, exc_traceback)
        error_msg = f"❌ 치명적인: 예약 프로세스 중 치명적인 오류 발생: {exc_value}\n{''.join(traceback_details)}"
        log_message(error_msg, message_queue)
        message_queue.put(
            f"🚨UI_ERROR:[{datetime.datetime.now(KST).strftime('%H:%M:%S.%f')[:-3]}] ❌ 치명적 오류 발생! 로그를 확인해주세요.")


# ============================================================
# Streamlit UI 구성 및 상태 관리
# ============================================================

# 🚨 세션 상태 초기화 및 기본값 설정
if 'log_messages' not in st.session_state:
    st.session_state.log_messages = ["프로그램 실행 준비 완료."]
if 'is_running' not in st.session_state:
    st.session_state.is_running = False
if 'stop_event' not in st.session_state:
    st.session_state.stop_event = threading.Event()
if 'booking_thread' not in st.session_state:
    st.session_state.booking_thread = None
if 'message_queue' not in st.session_state:
    st.session_state.message_queue = queue.Queue()
if 'inputs' not in st.session_state:
    st.session_state.inputs = {}
if 'run_id' not in st.session_state:
    st.session_state['run_id'] = None  # 실시간 업데이트 감시용 ID

# --- UI 입력 필드 초기화 (사용자 편의를 위한 기본값) ---
# KST 기준의 오늘 날짜로 get_default_date가 이미 설정됨
if 'id_input' not in st.session_state:
    st.session_state['id_input'] = ""
if 'pw_input' not in st.session_state:
    st.session_state['pw_input'] = ""
if 'date_input' not in st.session_state:
    st.session_state['date_input'] = get_default_date(28)
if 'run_date_input' not in st.session_state:
    st.session_state['run_date_input'] = get_default_date(0).strftime('%Y%m%d')
if 'run_time_input' not in st.session_state:
    st.session_state['run_time_input'] = "09:00:00"  # 사용자 요청 시간 반영
if 'res_start_input' not in st.session_state:
    st.session_state['res_start_input'] = "07:00"
if 'res_end_input' not in st.session_state:
    st.session_state['res_end_input'] = "09:00"
if 'course_input' not in st.session_state:
    st.session_state['course_input'] = "All"
if 'order_input' not in st.session_state:
    st.session_state['order_input'] = "역순 (내림)"
if 'delay_input' not in st.session_state:
    st.session_state['delay_input'] = "0"
if 'test_mode_checkbox' not in st.session_state:
    st.session_state['test_mode_checkbox'] = True
if 'log_container_placeholder' not in st.session_state:
    st.session_state['log_container_placeholder'] = None


def stop_booking():
    """메인 스레드에서 호출되어 UI를 업데이트하고 스레드를 중단시킵니다."""
    if not st.session_state.is_running: return
    # 스레드에 중단 신호 전달
    log_message("🛑 사용자가 '중단' 버튼을 클릭했습니다. 프로세스 종료 중...", st.session_state.message_queue)
    st.session_state.stop_event.set()
    st.session_state.is_running = False
    st.session_state['run_id'] = None  # 실행 ID 초기화
    st.rerun()


def run_booking():
    """'예약 시작' 버튼 핸들러 - 스레드 시작 및 실시간 업데이트 루프 실행"""
    if st.session_state.is_running:
        st.error("⚠️ 이미 예약 스레드가 실행 중입니다.")
        return

    # 1. 상태 및 Queue 초기화
    st.session_state.is_running = True
    st.session_state.stop_event.clear()
    st.session_state.log_messages = []
    st.session_state['run_id'] = time.time()  # 고유 실행 ID 설정 (실시간 업데이트 용)

    # 이전 큐 메시지 모두 비우기
    while not st.session_state.message_queue.empty():
        try:
            st.session_state.message_queue.get_nowait()
            st.session_state.message_queue.task_done()
        except:
            break

    # 입력 값 유효성 검사 (형식)
    try:
        datetime.datetime.strptime(st.session_state.run_date_input, '%Y%m%d')
        datetime.datetime.strptime(st.session_state.run_time_input, '%H:%M:%S')
    except ValueError:
        st.error("⚠️ 가동 시작일 또는 가동 시작 시간의 형식이 올바르지 않습니다. (YYYYMMDD / HH:MM:SS)")
        st.session_state.is_running = False
        st.session_state['run_id'] = None
        return

    # 폼 데이터 (st.session_state.inputs) 저장
    st.session_state.inputs = {
        'id': st.session_state.id_input,
        'pw': st.session_state.pw_input,
        # st.date_input의 value는 datetime.date 객체이며, API는 YYYYMMDD 형식 사용
        'date': st.session_state.date_input.strftime('%Y%m%d'),
        'run_date': st.session_state.run_date_input,
        'run_time': st.session_state.run_time_input,
        'start_time': st.session_state.res_start_input,
        'end_time': st.session_state.res_end_input,
        'course_type': st.session_state.course_input,
        'order': st.session_state.order_input,
        'delay': st.session_state.delay_input,
        'test_mode': st.session_state.test_mode_checkbox,
    }

    # 스레드 시작
    st.session_state.booking_thread = threading.Thread(
        target=start_pre_process,
        args=(st.session_state.message_queue, st.session_state.stop_event, st.session_state.inputs),
        daemon=True
    )
    st.session_state.booking_thread.start()

    # 메인 루프를 탈출하여 UI가 멈추지 않도록 합니다. (로그 업데이트를 위해 즉시 rerun)
    st.rerun()


def check_queue_and_rerun():
    """
    메인 스레드에서 실행되며, Queue를 감시하고 새 메시지가 있을 때마다
    UI를 업데이트(rerun)합니다.
    """
    if st.session_state['run_id'] is None:
        return

    new_message_received = False

    # Queue 처리 로직
    while not st.session_state.message_queue.empty():
        try:
            message = st.session_state.message_queue.get_nowait()
        except queue.Empty:
            break

        if message == "🚨UI_FINAL_STOP":
            st.session_state.is_running = False
            st.session_state['run_id'] = None
            new_message_received = True
            break

        elif message.startswith("🚨UI_ERROR:"):
            # UI_ERROR 메시지는 로그에만 표시하고 is_running을 False로 설정합니다.
            st.session_state.log_messages.append(message.replace("🚨UI_ERROR:", "[UI ALERT] ❌ "))
            st.session_state.is_running = False
            st.session_state.stop_event.set()
            st.session_state['run_id'] = None
            new_message_received = True
            break

        elif message.startswith("🚨UI_LOG:"):
            message_log = message.replace("🚨UI_LOG:", "")
            st.session_state.log_messages.append(message_log)
            new_message_received = True

    # 새 메시지가 들어왔거나 프로세스가 종료된 경우, UI를 새로고침합니다.
    if new_message_received or (not st.session_state.is_running and st.session_state['run_id'] is not None):

        if not st.session_state.is_running and st.session_state['run_id'] is not None:
            # 최종 종료 상태. 스레드가 아직 살아있으면 정리
            if st.session_state.booking_thread and st.session_state.booking_thread.is_alive():
                st.session_state.booking_thread.join(timeout=2)

            # run_id를 최종적으로 None으로 설정
            st.session_state['run_id'] = None
            # 최종 로그 업데이트를 위해 rerun
            st.rerun()
            return

        # UI 업데이트(로그 업데이트)를 위해 Streamlit 페이지를 새로고침합니다.
        st.rerun()

    # 실행 중일 경우, 0.1초마다 페이지를 새로고침하도록 Streamlit에 지시하여
    # UI가 멈추지 않고(Non-blocking) 실시간 업데이트되는 것처럼 보이게 합니다.
    if st.session_state.is_running and st.session_state['run_id'] is not None:
        time.sleep(0.1)
        st.rerun()


# -------------------------------------------------------------------------
# 💡 UI 레이아웃 (PC/모바일 최적화 - 입력 상자 너비 조정)
# -------------------------------------------------------------------------

st.set_page_config(
    layout="wide",
    menu_items=None
)

# CSS: PC/모바일 환경 모두에서 입력 필드의 최대 폭을 제한하여 입력 상자 길이를 짧게 만듭니다.
st.markdown("""
    <style>
    /* 🚨🚨🚨 1. 상단 간격 완전 제거 (최고 우선순위) 🚨🚨🚨 */
    /* stAppViewContainer의 섹션과 그 아래 VerticalBlock의 마진/패딩 제거 */
    div[data-testid="stAppViewContainer"] > section,
    div[data-testid="stVerticalBlock"] {
        margin-top: 0px !important;
        padding-top: 0px !important;
    }

    /* Streamlit 페이지 본문의 최상단 패딩 제거 (기존 캐시 클래스 포함) */
    .st-emotion-cache-1kyy013, .st-emotion-cache-1gh2stx, .main > div {
        padding-top: 0rem !important; 
    }

    /* 🚨🚨🚨 2. 타이틀 글씨 크기 및 마진 설정 🚨🚨🚨 */
    .app-title {
        font-size: 24px !important; /* !important로 우선순위 확보 */
        font-weight: bold; 
        margin-top: 0px !important;  /* 상단 마진도 0으로 강제 */
        margin-bottom: 5px;
    }

    /* 3. 기타 UI 요소 너비/여백 설정 유지 */
    div.stText, div.stDateInput, div.stSelectbox {
        max-width: 200px !important; 
    }
    div[data-testid="stTextInput"], div[data-testid="stDateInput"], div[data-testid="stSelectbox"] {
        max-width: 200px !important;
    }
    div[data-testid="stVerticalBlock"] > div:nth-child(1) > div:nth-child(1) > div {
        max-width: 420px;
    }
    .section-header {
        font-size: 16px; 
        font-weight: bold; 
        margin-top: 5px; 
        margin-bottom: 5px;
    }
    </style>
    """, unsafe_allow_html=True)

# 폰트 크기 축소 적용
st.markdown('<p class="app-title" style="font-size: 24px !important; margin-top: 0px !important;">⛳ 골드CC 모바일 예약</p>', unsafe_allow_html=True)

# --- 1. 설정 섹션 ---
with st.container(border=True):
    st.markdown('<p class="section-header">🔑 로그인 및 조건 설정</p>', unsafe_allow_html=True)

    # 1-1. 로그인 섹션 (2열 압축)
    col1, col2 = st.columns(2)
    with col1:
        st.text_input("사용자ID", key="id_input", label_visibility="visible")
    with col2:
        st.text_input("암호", type="password", key="pw_input", label_visibility="visible")

    # 1-2. 예약 및 가동 조건 (3열로 최대한 압축)
    st.markdown("---")
    st.markdown('<p class="section-header">🗓️ 예약/가동 시간 설정</p>', unsafe_allow_html=True)

    # 🚨 수정: 컬럼 비율을 [1, 1, 1]로 유지하되, CSS로 너비를 강제합니다.
    col3, col4, col5 = st.columns([1, 1, 1])

    with col3:
        # st.date_input은 높이가 높아, label을 짧게 변경
        st.date_input(
            "예약일",
            key="date_input",
            format="YYYY-MM-DD",
            label_visibility="visible"
        )

    with col4:
        # 가동 시작일
        st.text_input("가동시작일", key="run_date_input", help="YYYYMMDD", label_visibility="visible")

    with col5:
        # 가동 시작 시각
        st.text_input("가동시작시간", key="run_time_input", help="HH:MM:SS", label_visibility="visible")

    # 1-3. 필터 및 코스 설정 (3열 압축)
    st.markdown("---")
    st.markdown('<p class="section-header">⚙️ 티타임 필터 및 우선순위</p>', unsafe_allow_html=True)

    # 🚨 수정: 지연(초)와 테스트 모드의 컬럼 비율을 더 좁게 만듭니다. (예: [2.5, 2.5, 1])
    col6, col7, col8 = st.columns([2.5, 2.5, 1])

    with col6:
        st.text_input("시작시간", key="res_start_input", label_visibility="visible")
        st.selectbox("코스선택", ["All", "참피온", "마스타"], key="course_input", label_visibility="visible")

    with col7:
        st.text_input("종료시간", key="res_end_input", label_visibility="visible")
        st.selectbox("예약순서", ["순차(▲)", "역순(▼)"], key="order_input", label_visibility="visible")

    with col8:
        # 지연시간과 테스트 모드는 세로로 배치
        st.text_input("예약지연(초)", key="delay_input", help="예약 가능 신호 감지 후 예약 시도 지연 시간", label_visibility="visible")
        # 체크박스의 너비를 좁은 컬럼에 맞춥니다.
        st.checkbox("테스트", key="test_mode_checkbox", help="실제 예약 안함")

# --- 2. 실행 버튼 섹션 ---
st.markdown("---")
col_start, col_stop = st.columns(2)

with col_start:
    st.button(
        "🚀 예약 시작",
        on_click=run_booking,
        disabled=st.session_state.is_running,
        type="primary"
    )

with col_stop:
    st.button(
        "🛑 중단",
        on_click=stop_booking,
        disabled=not st.session_state.is_running,
        type="secondary"
    )

# --- 3. 로그 섹션 ---
st.markdown("---")
st.markdown('<p class="section-header">📝 실행 로그</p>', unsafe_allow_html=True)

# 로그 출력을 위한 Placeholder 생성
if st.session_state.log_container_placeholder is None:
    st.session_state.log_container_placeholder = st.empty()

# 로그 메시지 출력 (가장 최근 메시지가 위로 오도록 역순 출력)
# 로그 창 높이를 250px로 최적화
with st.session_state.log_container_placeholder.container(height=250):
    # 로그가 너무 길어지는 것을 방지하기 위해 최근 500줄만 표시
    for msg in reversed(st.session_state.log_messages[-500:]):

        # HTML 태그 충돌 방지
        safe_msg = msg.replace("<", "&lt;").replace(">", "&gt;")

        # UI_ERROR일 경우 붉은색 텍스트로 강조 표시
        if "[UI ALERT] ❌" in msg:
            # st.markdown을 사용하여 p 태그에 margin과 font-size를 직접 적용하여 간격 최소화
            st.markdown(f'<p style="font-size: 11px; margin: 0px; color: red; font-family: monospace;">{safe_msg}</p>',
                        unsafe_allow_html=True)
        # 성공/완료 메시지일 경우 녹색 텍스트로 강조 표시
        elif "🎉" in msg or "✅" in msg and "대기중" not in msg:
            # st.markdown을 사용하여 p 태그에 margin과 font-size를 직접 적용하여 간격 최소화
            st.markdown(
                f'<p style="font-size: 11px; margin: 0px; color: green; font-family: monospace;">{safe_msg}</p>',
                unsafe_allow_html=True)
        # 세션 유지 메시지 강조
        elif "💚 [세션 유지]" in msg:
            st.markdown(
                f'<p style="font-size: 11px; margin: 0px; color: #008080; font-family: monospace;">{safe_msg}</p>',
                unsafe_allow_html=True)
        else:
            # st.markdown을 사용하여 p 태그에 margin과 font-size를 직접 적용하여 간격 최소화
            st.markdown(f'<p style="font-size: 11px; margin: 0px; font-family: monospace;">{safe_msg}</p>',
                        unsafe_allow_html=True)

# 실시간 업데이트를 위한 Queue 감시 함수 호출
check_queue_and_rerun()
