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
from concurrent.futures import ThreadPoolExecutor, as_completed

# InsecureRequestWarning 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================
# Utility Functions
# ============================================================

def log_message(message, message_queue):
    """백그라운드 스레드에서 호출. 메시지를 Queue에 넣습니다."""
    try:
        # 🚨 UI_LOG 접두사를 사용하여 메인 스레드에서 UI 업데이트에 사용됩니다.
        message_queue.put(f"🚨UI_LOG:[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {message}")
    except Exception:
        pass


def get_default_date(days):
    """오늘 날짜로부터 지정된 일수만큼 지난 날짜를 반환 (datetime.date 객체)"""
    return (datetime.date.today() + datetime.timedelta(days=days))


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


def wait_until(target_dt, stop_event, message_queue, log_prefix="프로그램 실행", log_countdown=False):
    """특정 시간까지 대기 (쓰레드 내에서 실행)"""

    log_message(f"⏳ {log_prefix} 대기중: {target_dt.strftime('%H:%M:%S.%f')[:-3]} (로컬 시스템 시간)", message_queue)
    last_remaining_sec = None
    # 🚨 카운트다운 시작 시점을 30초 전으로 고정합니다.
    log_remaining_start = 30

    while not stop_event.is_set():
        now = datetime.datetime.now()
        remaining_seconds = (target_dt - now).total_seconds()

        if remaining_seconds <= 0.05:  # 0.05초 여유를 두고 탈출
            break

        current_remaining_sec = int(remaining_seconds)
        # 🚨 log_countdown이 True이고 30초 이하일 경우에만 카운트다운 표시
        if log_countdown and remaining_seconds <= log_remaining_start:
            if current_remaining_sec > 0 and current_remaining_sec != last_remaining_sec:
                log_message(f"⏳ 예약 대기시간: {current_remaining_sec}초", message_queue)
                last_remaining_sec = current_remaining_sec

        if remaining_seconds < 1:
            time.sleep(0.005)
        else:
            # 🚨 Streamlit UI 갱신을 위해 메인 스레드에 제어권을 주기 위한 짧은 sleep 유지
            time.sleep(0.1)

    if not stop_event.is_set():
        log_message(f"✅ 목표 시간 도달! {log_prefix} 스레드 즉시 실행.", message_queue)


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
        # 🚨 msNum을 None으로 초기화하고 동적으로 추출
        self.ms_num = None

    def log_message(self, msg):
        self.log_message_func(msg, self.message_queue)

    def requests_login(self, usrid, usrpass, max_retries=3):
        """순수 requests를 이용한 API 로그인 시도"""
        login_url = "https://www.gakorea.com/controller/MemberController.asp"
        headers = {
            # 원본 코드 유지
            "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
            "Referer": "https://www.gakorea.com/mobile/join/login.asp",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"
        }
        payload = {
            "method": "doLogin", "coDiv": "undefined", "id": usrid, "pw": usrpass
        }

        for attempt in range(max_retries):
            if self.stop_event.is_set(): return {'result': 'fail', 'cookies': {}}
            try:
                self.log_message(f"🔄 API 로그인 시도 중... (시도 {attempt + 1}/{max_retries})")
                res = self.session.post(login_url, headers=headers, data=payload, timeout=10, verify=False,
                                        allow_redirects=False)

                cookies = dict(self.session.cookies)
                if res.status_code == 200 and any('SESSIONID' in key for key in cookies):
                    return {'result': 'success', 'cookies': cookies}

                self.log_message(f"❌ API 로그인 실패 (HTTP {res.status_code} 또는 쿠키 추출 실패).")
                if attempt < max_retries - 1: time.sleep(0.1)
            except requests.RequestException as e:
                self.log_message(f"❌ 네트워크 오류: 로그인 중 오류 발생: {e}")
                if attempt < max_retries - 1: time.sleep(0.1)
            except Exception as e:
                self.log_message(f"❌ 예외 오류: 로그인 중 알 수 없는 오류 발생: {e}")
                break

        return {'result': 'fail', 'cookies': {}}

    def extract_ms_num(self):
        """웹페이지에서 msNum 값을 동적으로 추출 (타임아웃 15초 및 디버그 로그 강화)"""
        target_url = "https://www.gakorea.com/reservation/golf/reservation.asp"
        headers = {
            # User-Agent는 모바일 브라우저로 위장하여 서버가 모바일 페이지를 주도록 유도합니다.
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Referer": "https://www.gakorea.com/mobile/join/login.asp",
            "Connection": "keep-alive"
        }
        self.log_message(f"🔎 웹 페이지 ({target_url})에서 msNum 값 추출 시도 중...")

        match = None
        try:
            # 타임아웃을 15초로 증가 (네트워크 지연 대비)
            res = self.session.get(target_url, headers=headers, timeout=15, verify=False)

            # --- 디버그 로그: 상태 코드 확인 ---
            self.log_message(f"✅ 추출 페이지 응답: Status {res.status_code}")
            if res.status_code != 200:
                self.log_message(f"⚠️ 요청 실패: 상태 코드가 200이 아님. HTML 앞 100자: {res.text[:100]}...")
            # ----------------------------------

            # --- 강화된 정규 표현식 패턴 (3단계 검색) ---
            # 1. HTML Hidden 필드 패턴 (가장 일반적)
            pattern1 = r'name=["\']msNum["\']\s*value\s*=\s*["\']?(\d+)'
            match = re.search(pattern1, res.text, re.DOTALL | re.IGNORECASE)

            # 2. JavaScript 변수 할당 패턴 (대체)
            if not match:
                # msNum: '숫자', msNum = '숫자', 또는 함수 호출 인자 형태를 찾습니다.
                pattern2 = r'msNum\s*[:=]\s*["\']?(\d{10,})'  # 10자리 이상 숫자만 타겟
                match = re.search(pattern2, res.text, re.IGNORECASE)

            # 3. 전체 HTML에서 msNum과 긴 숫자 ID 매칭 (최후의 수단)
            if not match:
                # 'msNum' 텍스트 뒤에 50자 이내에 있는 10자리 이상의 숫자를 찾습니다.
                pattern3 = r'msNum[\s\S]{0,50}(\d{10,})'
                match = re.search(pattern3, res.text, re.IGNORECASE)

            if match:
                self.ms_num = match.group(1)
                self.log_message(f"✅ msNum 추출 성공: {self.ms_num}")
                return True
            else:
                self.log_message("❌ msNum 패턴을 찾을 수 없습니다. HTML 구조 변경 가능성.")
                # --- 디버그 로그: 추출 실패 시 HTML 내용 출력 ---
                self.log_message(f"❌ 추출 실패 HTML 내용 앞 200자: {res.text[:200]}")
                # ------------------------------------------
                return False

        except requests.RequestException as e:
            self.log_message(f"❌ msNum 추출 오류: 네트워크 요청 실패/타임아웃 ({e})")
            return False

    def keep_session_alive(self, target_dt):
        """세션 유지를 위해 1분에 1회 서버에 접속 시도 (백그라운드 스레드에서 실행)"""

        # 예약 시작 시간 전까지만 세션 유지 시도
        while not self.stop_event.is_set() and datetime.datetime.now() < target_dt:

            # 🚨 1분에 1회 (60초)마다 세션 유지 시도
            time_to_sleep = 60.0

            try:
                # 캘린더 조회 API는 가볍고 세션 유지를 위한 용도로 적합합니다.
                # 참고: date는 오늘 날짜로 임의 설정
                current_date = datetime.date.today().strftime('%Y%m%d')
                if self.check_booking_open_by_calendar(current_date):
                    self.log_message("💚 [세션 유지] 성공적으로 세션 유지 신호를 서버에 보냈습니다.")
                else:
                    self.log_message("⚠️ [세션 유지] 신호 전송 실패 또는 예약 정보 확인 실패.")

            except Exception as e:
                self.log_message(f"❌ [세션 유지] 통신 오류 발생: {e}")

            # 다음 시도까지 대기
            i = 0
            while i < time_to_sleep and not self.stop_event.is_set() and datetime.datetime.now() < target_dt:
                time.sleep(1)  # 1초씩 짧게 쉬면서 중단 신호 확인
                i += 1

        if self.stop_event.is_set():
            self.log_message("🛑 세션 유지 스레드: 중단 신호 감지. 종료합니다.")
        else:
            self.log_message("✅ 세션 유지 스레드: 예약 정시 도달. 종료합니다.")

    def check_booking_open_by_calendar(self, date):
        """해당일 '예약가능' 버튼 생성 여부 확인"""
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.gakorea.com",
            "Referer": "https://www.gakorea.com/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }
        year_month = date[:6]

        # 🚨 self.ms_num이 추출된 값인지 확인
        if self.ms_num is None:
            self.log_message("❌ check_booking_open_by_calendar: msNum이 설정되지 않았습니다.", self.message_queue)
            return False

        payload = {
            "method": "getCalendar", "coDiv": "611", "selYm": year_month, "msNum": self.ms_num, "msDivision": "10",
            "msClass": "01", "msLevel": "00"
        }

        try:
            res = self.session.post(url, headers=headers, data=payload, timeout=2, verify=False)
            res.raise_for_status()
            data = json.loads(res.text)

            if 'rows' in data and data['rows']:
                for day_info in data['rows']:
                    if day_info.get('CL_SOLAR') == date:
                        openday = day_info.get('OPENDAY', '99999999')
                        if openday != '99999999': return True
                return False
            return False
        except requests.RequestException:
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
            "Referer": "https://www.gakorea.com/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }
        cos_values = ["A", "B", "C", "D"]
        all_fetched_times = []

        # 🚨 msNum이 추출된 값인지 확인
        if self.ms_num is None:
            self.log_message("❌ get_all_available_times: msNum이 설정되지 않았습니다.", self.message_queue)
            return []

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
            "Referer": "https://www.gakorea.com/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }

        part = "1" if cos in ["A", "C"] else "2"
        # 🚨 self.ms_num을 사용
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

        # --- 🚨 상위 5개 시간대 전체 표시 ---
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
            "Referer": "https://www.gakorea.com/reservation/golf/reservation.asp",
            "Connection": "keep-alive"
        }
        time_for_api = format_time_for_api(time_)
        # 🚨 self.ms_num을 사용
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
                    self.log_message(f"👍 예약 성공: {course} {time_} (시도 {attempt + 1}/{max_retries})")
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
        self.log_message(f"\n[API EXEC] 🚀 API 예약 프로세스 즉시 시작!")

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
    """백그라운드 스레드에서 실행되는 예약 핵심 로직 (시간 제어 포함)"""

    log_message("[INFO] ⚙️ 예약 시작 조건 확인 완료.", message_queue)

    try:
        params = inputs
        core = APIBookingCore(log_message, message_queue, stop_event)

        # 1. 로그인
        log_message("[API] ⚙️ API 로그인 시작...", message_queue)
        login_result = core.requests_login(params['id'], params['pw'])
        if login_result['result'] != 'success':
            log_message("❌ 로그인 실패. 아이디/비밀번호를 확인하거나 네트워크를 점검하세요.", message_queue)
            message_queue.put(f"🚨UI_ERROR:로그인 실패: 아이디 또는 비밀번호를 확인하세요.")
            return

        log_message("🔑 순수 API 로그인 완료. 세션 쿠키 추출 성공.", message_queue)

        # 2. msNum 동적 추출
        if not core.extract_ms_num():
            log_message("❌ msNum 추출 실패. 예약 프로세스를 중단합니다.", message_queue)
            message_queue.put(f"🚨UI_ERROR:msNum 추출 오류: msNum 값을 가져올 수 없어 예약을 중단합니다.")
            return

        log_message(f"✅ 동적 msNum({core.ms_num}) 확보 완료.", message_queue)

        # 3. 가동 시작 시간 계산
        # st.date_input의 형식이 YYYY-MM-DD이지만, inputs에는 YYYYMMDD로 저장
        run_datetime_str = f"{params['run_date']} {params['run_time']}"
        run_datetime = datetime.datetime.strptime(run_datetime_str, '%Y%m%d %H:%M:%S')
        # 🚨 예약 시간 1분 전(60초 전)에 티 타임 조회 시작
        pre_fetch_time = run_datetime - datetime.timedelta(seconds=60)

        # --- 🚨 수정된 부분 1: 세션 유지 스레드 실행 ---
        session_thread = threading.Thread(
            target=core.keep_session_alive,
            args=(run_datetime,),  # 예약 정시까지 세션 유지
            daemon=True
        )
        session_thread.start()
        log_message("✅ 세션 유지 스레드 시작. 1분마다 세션 유지를 시도합니다.", message_queue)
        # -----------------------------------------------

        # 4. 1분전까지 대기 (카운트다운 없음)
        log_message(f"⏳ 티 타임 조회 대기중. 목표 시각: {pre_fetch_time.strftime('%H:%M:%S')}", message_queue)
        # 🚨 티 타임 조회 대기 시점에는 카운트다운 X
        wait_until(pre_fetch_time, stop_event, message_queue, log_prefix="티 타임 조회", log_countdown=False)
        if stop_event.is_set(): return

        # 🚨 티 타임 조회
        all_times = core.get_all_available_times(params['date'])

        is_reverse_order = params['order'] == '역순 (내림)'

        sorted_times = core.filter_and_sort_times(
            all_times, params['res_start'], params['res_end'], params['course'], is_reverse_order
        )

        if not sorted_times:
            log_message("[NO MATCH] 설정된 조건에 맞는 예약 가능 시간이 없습니다. 예약 시도 중단.", message_queue)
            message_queue.put(
                f"🚨UI_ERROR:예약 가능 시간대 없음: 필터링 조건({params['res_start']}~{params['res_end']})에 맞는 시간이 없습니다.")
            return

        # 5. 가동 시작 시간까지 대기 및 예약 가능 버튼 감시
        log_message(f"⏳ 예약 시작 정시 대기중. 목표 시각: {run_datetime.strftime('%H:%M:%S')}", message_queue)

        # --- 🚨 수정된 부분 2: 예약 정시까지 대기 시에만 카운트다운 (30초 전부터) ---
        # log_countdown=True로 설정하여 30초 전부터 "예약 대기시간: ??초"를 표시합니다.
        wait_until(run_datetime, stop_event, message_queue, log_prefix="예약 가능 버튼 감시", log_countdown=True)
        # ----------------------------------------------------------------------

        if stop_event.is_set(): return

        log_message("🔍 [감시 시작] 골드CC 서버의 '예약가능' 버튼 생성을 감시합니다.", message_queue)

        watchdog_count = 0
        while not stop_event.is_set():
            watchdog_count += 1
            is_open = core.check_booking_open_by_calendar(params['date'])

            if is_open:
                log_message(f"✅ [감지 성공] 예약 가능 신호 감지! ({watchdog_count}회 시도)", message_queue)
                break

            if watchdog_count > 100:
                log_message("⚠️ 예약 가능 신호 감시 실패: 최대 감시 횟수를 초과했습니다 (100회).", message_queue)
                message_queue.put(f"🚨UI_ERROR:감시 실패: 예약 가능 신호 감지 실패 (최대 시도 횟수 초과).")
                return

            # 짧게 쉬면서 UI 스레드에 제어권 양보
            time.sleep(0.1)

        if stop_event.is_set(): return

        # 6. 예약 시도
        success = core.run_api_booking(
            params['date'], params['course'], params['test_mode'], sorted_times, float(params['delay'])
        )

        if success:
            log_message("🎉 예약 프로세스 성공적으로 완료.", message_queue)
        else:
            log_message("❌ 예약 프로세스 실패.", message_queue)

    except ValueError as ve:
        log_message(f"[입력 오류] ❌ {ve}", message_queue)
        message_queue.put(f"🚨UI_ERROR:입력 오류: 날짜/시간 형식을 확인하세요.")
    except Exception as e:
        log_message(f"[Main Process] ❌ 치명적인 오류 발생: {e}", message_queue)
        traceback.print_exc()
        message_queue.put(f"🚨UI_ERROR:치명적 오류: {e}")

    finally:
        message_queue.put("🚨UI_FINAL_STOP")


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
if 'id_input' not in st.session_state:
    st.session_state['id_input'] = ""
if 'pw_input' not in st.session_state:
    st.session_state['pw_input'] = ""
if 'date_input' not in st.session_state:
    st.session_state['date_input'] = get_default_date(28)
if 'run_date_input' not in st.session_state:
    st.session_state['run_date_input'] = get_default_date(0).strftime('%Y%m%d')
if 'run_time_input' not in st.session_state:
    st.session_state['run_time_input'] = "08:45:00"  # 사용자 요청 시간 반영
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

    # 폼 데이터를 session_state.inputs에 저장
    st.session_state.inputs = {
        'id': st.session_state.id_input,
        'pw': st.session_state.pw_input,
        # st.date_input의 value는 datetime.date 객체이며, API는 YYYYMMDD 형식 사용
        'date': st.session_state.date_input.strftime('%Y%m%d'),
        'run_date': st.session_state.run_date_input,
        'run_time': st.session_state.run_time_input,
        'res_start': st.session_state.res_start_input,
        'res_end': st.session_state.res_end_input,
        'course': st.session_state.course_input,
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
    🚨 메인 스레드에서 실행되며, Queue를 감시하고 새 메시지가 있을 때마다
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
    if new_message_received or not st.session_state.is_running:

        if not st.session_state.is_running and st.session_state['run_id'] is None:
            # 최종 종료 상태. 스레드가 아직 살아있으면 정리
            if st.session_state.booking_thread and st.session_state.booking_thread.is_alive():
                st.session_state.booking_thread.join(timeout=2)

            # 최종 로그 업데이트를 위해 rerun
            st.rerun()
            return

        # UI 업데이트(로그 업데이트)를 위해 Streamlit 페이지를 새로고침합니다.
        st.rerun()

    # 🚨 실행 중일 경우, 0.1초마다 페이지를 새로고침하도록 Streamlit에 지시하여
    # UI가 멈추지 않고(Non-blocking) 실시간 업데이트되는 것처럼 보이게 합니다.
    if st.session_state.is_running and st.session_state['run_id'] is not None:
        time.sleep(0.1)
        st.rerun()


# -------------------------------------------------------------------------
# UI 레이아웃
# -------------------------------------------------------------------------

st.set_page_config(layout="wide")
st.title("⛳ 골드CC 모바일 예약")

# 🚨 상단 상태 메시지 출력 제거 (문제 3번 해결)

# --- 1. 설정 섹션 ---
with st.container(height=500, border=True):
    st.subheader("🔑 로그인 및 조건 설정")

    col1, col2 = st.columns(2)
    with col1:
        st.text_input("아이디", key="id_input")
    with col2:
        st.text_input("비밀번호", type="password", key="pw_input")

    st.markdown("---")

    # 🗓️ 예약 및 가동 조건
    col3, col4 = st.columns([0.7, 0.3])
    with col3:
        st.date_input(
            "예약 목표일",
            key="date_input",
            format="YYYY-MM-DD"
        )
    with col4:
        st.text_input("가동 시작일 (YYYYMMDD)", key="run_date_input")
        st.text_input("가동 시작 시각 (HH:MM:SS)", key="run_time_input")

    st.markdown("---")

    # 🕒 시간 범위 필터 및 코스 설정
    col5, col6, col7 = st.columns(3)
    with col5:
        st.text_input("예약 시작시간 (HH:MM)", key="res_start_input")
        st.selectbox("코스", ["All", "참피온", "마스타"], key="course_input")
    with col6:
        st.text_input("예약 종료시간 (HH:MM)", key="res_end_input")
        st.selectbox("우선순위", ["순차 (오름)", "역순 (내림)"], key="order_input")
    with col7:
        st.text_input("예약 지연시간 (초)", key="delay_input", help="예약 가능 신호 감지 후 예약 시도 지연 시간")
        st.checkbox("테스트 모드 (실제 예약 안함)", key="test_mode_checkbox")

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
st.subheader("📝 실행 로그")

# 🚨 로그 출력을 위한 Placeholder 생성
if st.session_state.log_container_placeholder is None:
    st.session_state.log_container_placeholder = st.empty()

# 로그 메시지 출력 (가장 최근 메시지가 위로 오도록 역순 출력)
with st.session_state.log_container_placeholder.container(height=250):
    # 로그가 너무 길어지는 것을 방지하기 위해 최근 500줄만 표시
    for msg in reversed(st.session_state.log_messages[-500:]):

        # HTML 태그 충돌 방지
        safe_msg = msg.replace("<", "&lt;").replace(">", "&gt;")

        # UI_ERROR일 경우 붉은색 텍스트로 강조 표시
        if "[UI ALERT] ❌" in msg:
            # 🚨 st.markdown을 사용하여 p 태그에 margin과 font-size를 직접 적용하여 간격 최소화
            st.markdown(f'<p style="font-size: 11px; margin: 0px; color: red; font-family: monospace;">{safe_msg}</p>',
                        unsafe_allow_html=True)
        # 성공/완료 메시지일 경우 녹색 텍스트로 강조 표시
        elif "🎉" in msg or "✅" in msg and "대기중" not in msg:
            # 🚨 st.markdown을 사용하여 p 태그에 margin과 font-size를 직접 적용하여 간격 최소화
            st.markdown(
                f'<p style="font-size: 11px; margin: 0px; color: green; font-family: monospace;">{safe_msg}</p>',
                unsafe_allow_html=True)
        # 세션 유지 메시지 강조
        elif "💚 [세션 유지]" in msg:
            st.markdown(
                f'<p style="font-size: 11px; margin: 0px; color: #008080; font-family: monospace;">{safe_msg}</p>',
                unsafe_allow_html=True)
        else:
            # 🚨 st.markdown을 사용하여 p 태그에 margin과 font-size를 직접 적용하여 간격 최소화
            st.markdown(f'<p style="font-size: 11px; margin: 0px; font-family: monospace;">{safe_msg}</p>',
                        unsafe_allow_html=True)

# 🚨 실시간 업데이트를 위한 Queue 감시 함수 호출
check_queue_and_rerun()
