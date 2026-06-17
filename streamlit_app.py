import streamlit as st

# 페이지 설정 (최상단 배치)
st.set_page_config(
    page_title="골드CC 모바일 예약",
    page_icon="⛳",
    layout="wide",
)

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

# KST 시간대 객체 정의
KST = pytz.timezone('Asia/Seoul')


# ============================================================
# Utility Functions
# ============================================================

def log_message(message, message_queue):
    """백그라운드 스레드에서 UI 로그 큐에 메시지 삽입"""
    try:
        now_kst = datetime.datetime.now(KST)
        timestamp = now_kst.strftime('%H:%M:%S.%f')[:-3]
        message_queue.put(f"🚨UI_LOG:[{timestamp}] {message}")
    except Exception:
        pass


def get_default_date(days):
    """기준일로부터 n일 뒤 날짜 반환"""
    return (datetime.datetime.now(KST).date() + datetime.timedelta(days=days))


def format_time_for_api(time_str):
    """HH:MM -> HHMM 변환"""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{3,4}$', time_str) and time_str.isdigit():
        return time_str.zfill(4)
    return "0000"


def format_time_for_display(time_str):
    """HHMM -> HH:MM 변환"""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{4}$', time_str) and time_str.isdigit():
        return f"{time_str[:2]}:{time_str[2:]}"
    return time_str


def wait_until(target_dt_kst, stop_event, message_queue, log_prefix="프로그램 실행", log_countdown=False):
    """특정 시간까지 정밀 대기"""
    log_message(f"⏳ {log_prefix} 대기중: {target_dt_kst.strftime('%H:%M:%S.%f')[:-3]} (KST 기준)", message_queue)
    last_remaining_sec = None
    while not stop_event.is_set():
        now_kst = datetime.datetime.now(KST)
        remaining_seconds = (target_dt_kst - now_kst).total_seconds()

        if remaining_seconds <= 0.001:
            break

        if log_countdown and remaining_seconds <= 30:
            current_remaining_sec = int(remaining_seconds)
            if current_remaining_sec != last_remaining_sec:
                log_message(f"⏳ 예약 시작 대기중 ({current_remaining_sec}초)", message_queue)
                last_remaining_sec = current_remaining_sec

        if remaining_seconds < 0.1:
            time.sleep(0.001)
        else:
            time.sleep(0.05)


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
        self.KST = pytz.timezone('Asia/Seoul')

    def log_message(self, msg):
        self.log_message_func(msg, self.message_queue)

    def requests_login(self, usrid, usrpass):
        """API 로그인 및 msNum 추출"""
        login_url = "https://www.gakorea.com/controller/MemberController.asp"
        payload = {"method": "doLogin", "id": usrid, "pw": usrpass}
        try:
            res = self.session.post(login_url, data=payload, timeout=10, verify=False)
            match = re.search(r'(?:msNum|ms_num)\s*[:=]\s*["\']?(\d{10,})["\']?', res.text, re.IGNORECASE)
            if match:
                self.ms_num = match.group(1)
                return True
        except Exception:
            pass
        return False

    def keep_session_alive(self, target_dt):
        """정해진 시간까지 1분마다 세션 유지 요청"""
        self.log_message("✅ 세션 유지 스레드 시작 (1분 주기).")
        while not self.stop_event.is_set() and datetime.datetime.now(self.KST) < target_dt:
            try:
                self.session.get("https://www.gakorea.com/mobile/join/login.asp", timeout=5, verify=False)
                self.log_message("💚 [세션 유지] 서버 연결 확인 (1분주기).")
            except Exception:
                pass

            rem = (target_dt - datetime.datetime.now(self.KST)).total_seconds()
            if self.stop_event.wait(timeout=min(60, rem)):
                break
        self.log_message("✅ 세션 유지 스레드 종료.")

    def check_booking_open_by_calendar(self, date):
        """캘린더 API를 통한 예약 오픈 여부 확인"""
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        payload = {
            "method": "getCalendar", "coDiv": "611", "selYm": date[:6],
            "msNum": self.ms_num, "msDivision": "10", "msClass": "01", "msLevel": "00"
        }
        try:
            res = self.session.post(url, data=payload, timeout=3.0, verify=False)
            # 해당 날짜 정보가 있고 OPENDAY가 설정되어 있는지 확인
            return date in res.text and '"OPENDAY":"99999999"' not in res.text
        except Exception:
            return False

    def get_all_available_times(self, date):
        """모든 코스 티타임 조회 (멀티스레드)"""
        all_times = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(self._fetch_tee_list, date, cos) for cos in ["A", "B", "C", "D"]]
            for future in as_completed(futures):
                all_times.extend(future.result())
        return all_times

    def _fetch_tee_list(self, date, cos):
        """단일 코스 티 리스트 조회"""
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        part = "1" if cos in ["A", "C"] else "2"
        payload = {
            "method": "getTeeList", "coDiv": "611", "date": date, "cos": cos, "part": part,
            "msNum": self.ms_num, "msDivision": "10", "msClass": "01", "msLevel": "00"
        }
        try:
            res = self.session.post(url, data=payload, timeout=3.0, verify=False)
            data = json.loads(res.text)
            times = [
                (t['BK_TIME'], t['BK_COS'], t['BK_PART'], self.course_detail_mapping.get(cos, 'Unknown'), "611")
                for t in data.get('rows', [])
            ]
            self.log_message(f"🔍 getTeeList 완료 (cos={cos}): {len(times)}개 시간대")
            return times
        except Exception:
            return []

    def try_reservation(self, date, course, time_val, bk_cos, bk_part, co_div):
        """실제 예약 API 호출"""
        url = "https://www.gakorea.com/controller/ReservationController.asp"
        payload = {
            "method": "doReservation", "coDiv": co_div, "day": date, "cos": bk_cos,
            "time": format_time_for_api(time_val), "tCnt": "0", "msNum": self.ms_num,
            "msDivision": "10", "msClass": "01", "msLevel": "00", "media": "R", "gubun": "M",
            "_": str(int(time.time() * 1000))
        }
        try:
            res = self.session.post(url, data=payload, timeout=5.0, verify=False)
            data = json.loads(res.text)
            if data.get('resultCode') == '0000':
                return True
            else:
                self.log_message(f"⚠️ 실패 사유: {data.get('resultMsg', '알 수 없음')}")
                return False
        except Exception:
            return False

    def run_api_booking(self, date, test_mode, sorted_times, delay):
        """예약 시도 실행 로직 (상위 순위 목록 표시 추가)"""

        # [추가] 예약 시도 전, 상위 3순위 목록을 로그에 먼저 표시
        targets = sorted_times[:3]
        if targets:
            self.log_message(
                f"📋 예약 타겟: " + ", ".join([f"[{i + 1}]{format_time_for_display(t[0])}" for i, t in enumerate(targets)]))

        if delay > 0:
            self.log_message(f"⏳ 예약 지연 {delay}초 대기 중...")
            time.sleep(delay)

        for i, (t_val, b_cos, b_part, c_nm, c_div) in enumerate(sorted_times[:5]):
            if self.stop_event.is_set(): break

            disp_t = format_time_for_display(t_val)
            self.log_message(f"➡️ [{i + 1}순위] 시도: {c_nm} {disp_t}")

            if test_mode:
                self.log_message(f"✅ [테스트 모드] {c_nm} {disp_t} 예약 가능 확인됨.")
                return True

            if self.try_reservation(date, c_nm, disp_t, b_cos, b_part, c_div):
                self.log_message(f"🎉🎉🎉 예약 성공!!! {c_nm} {disp_t} 🎉🎉🎉")
                return True

            # [추가] 실패 시 다음 순위가 있다면 로그 표시
            elif i < 4 and i < len(sorted_times) - 1:
                self.log_message(f"⚠️ {i + 1}순위 실패, 다음 순위로 넘어갑니다.")

        return False

# ============================================================
# Main Processing Logic (수정됨)
# ============================================================
def start_pre_process(message_queue, stop_event, inputs):
    try:
        core = APIBookingCore(log_message, message_queue, stop_event)

        # 1. 로그인
        log_message("✅ 작업 진행 중: API 로그인 시도...", message_queue)
        if not core.requests_login(inputs['id'], inputs['pw']):
            message_queue.put("🚨UI_ERROR:로그인 실패: ID/PW를 확인하세요.")
            return
        log_message("✅ 로그인 및 msNum 확보 성공.", message_queue)

        # 2. 시간 설정
        run_dt = KST.localize(
            datetime.datetime.strptime(f"{inputs['run_date']} {inputs['run_time']}", '%Y%m%d %H:%M:%S'))

        # 3. 세션 유지 스레드 시작
        threading.Thread(target=core.keep_session_alive, args=(run_dt,), daemon=True).start()

        # 4. 정시 대기 (30초 전부터 카운트다운)
        if datetime.datetime.now(KST) < run_dt:
            wait_until(run_dt, stop_event, message_queue, log_prefix="최종 예약 시도", log_countdown=True)

        if stop_event.is_set(): return

        # 5. 티 타임 조회 및 필터링
        log_message("🔎 티 타임 조회 시작...", message_queue)
        all_times = core.get_all_available_times(inputs['date'])

        s_limit = format_time_for_api(inputs['start_time'])
        e_limit = format_time_for_api(inputs['end_time'])
        c_filter = {"All": ["A", "B", "C", "D"], "참피온": ["A", "B"], "마스타": ["C", "D"]}.get(inputs['course_type'], [])

        filtered = [t for t in all_times if s_limit <= format_time_for_api(t[0]) <= e_limit and t[1] in c_filter]
        filtered.sort(key=lambda x: int(x[0]), reverse=(inputs['order'] == "역순(▼)"))

        if not filtered:
            log_message("⚠️ 조건에 맞는 티가 없습니다. 프로세스를 종료합니다.", message_queue)
            return

        log_message(f"✅ 총 {len(filtered)}개의 예약 가능 타임 확보.", message_queue)
        log_message(f"📜 1순위 타겟: {format_time_for_display(filtered[0][0])} ({filtered[0][3]})", message_queue)

        # 6. 예약 오픈 감지
        log_message("🚀 예약 오픈 감지 시작...", message_queue)
        start_wait = time.monotonic()
        while not stop_event.is_set() and (time.monotonic() - start_wait < 420):
            if core.check_booking_open_by_calendar(inputs['date']) or len(filtered) > 0:
                log_message("🎉 예약 신호 감지 성공! 즉시 예약을 시작합니다.", message_queue)
                core.run_api_booking(inputs['date'], inputs['test_mode'], filtered, int(inputs['delay']))
                break
            time.sleep(0.01)

    except Exception as e:
        log_message(f"❌ 치명적 오류: {str(e)}", message_queue)
        message_queue.put("🚨UI_ERROR:작업 중 오류 발생")
    finally:
        # 핵심 추가: 작업이 정상 종료되든 오류가 나든 UI에 종료를 알림
        message_queue.put("🚨UI_FINISH:")


# ============================================================
# Streamlit UI (로직 보완)
# ============================================================

# 세션 상태 초기화
if 'log_messages' not in st.session_state: st.session_state.log_messages = ["프로그램 준비 완료."]
if 'is_running' not in st.session_state: st.session_state.is_running = False
if 'stop_event' not in st.session_state: st.session_state.stop_event = threading.Event()
if 'message_queue' not in st.session_state: st.session_state.message_queue = queue.Queue()

# UI 레이아웃
st.markdown('<p style="font-size: 26px; font-weight: bold; text-align: center;">⛳ 골드CC 모바일 예약</p>',
            unsafe_allow_html=True)

with st.container(border=True):
    st.markdown("**🔑 설정**")
    c1, c2 = st.columns(2)
    id_in = c1.text_input("사용자ID", value="")
    pw_in = c2.text_input("암호", type="password", value="")

    st.markdown("---")
    c3, c4, c5 = st.columns(3)
    date_in = c3.date_input("예약희망일", value=get_default_date(28))
    run_date_in = c4.text_input("가동시작일(YYYYMMDD)", value=datetime.datetime.now(KST).strftime('%Y%m%d'))
    times = [f"{h:02}:{m:02}:00" for h in range(8, 20) for m in [0, 10, 20, 30, 40, 50]]
    run_time_in = c5.selectbox("가동시작시간", times, index=times.index("09:00:00"))

    st.markdown("---")
    c6, c7, c8 = st.columns([2, 2, 1])
    with c6:
        # ---- 수정된 부분 시작 ----
        time_list = [f"{h:02}:{m:02}" for h in range(6, 15) for m in (0, 30) if not (h == 14 and m == 30)]
        s_t = st.selectbox("조회 시작", time_list, index=3) # 07:30 디폴트 설정 (인덱스 3)
        e_t = st.selectbox("조회 종료", time_list, index=6) # 09:00 디폴트 설정
#        e_t = st.selectbox("조회 종료", time_list, index=len(time_list)-1) # 13:00 디폴트 설정
        # ---- 수정된 부분 끝 ----
    with c7:
        crs = st.selectbox("코스", ["All", "참피온", "마스타"])
        ordr = st.selectbox("순서", ["순차(▲)", "역순(▼)"], index=1)
    with c8:
        dly = st.text_input("지연(초)", value="0")
        tst = st.checkbox("테스트", value=True)

# 실행 버튼
bc1, bc2, _ = st.columns([1, 1, 4])
if bc1.button("🚀 예약 시작", type="primary", disabled=st.session_state.is_running):
    st.session_state.is_running = True
    st.session_state.stop_event.clear()
    st.session_state.log_messages = []

    inputs = {
        'id': id_in, 'pw': pw_in, 'date': date_in.strftime('%Y%m%d'),
        'run_date': run_date_in, 'run_time': run_time_in,
        'start_time': s_t, 'end_time': e_t, 'course_type': crs,
        'order': ordr, 'delay': dly, 'test_mode': tst
    }

    threading.Thread(target=start_pre_process,
                     args=(st.session_state.message_queue, st.session_state.stop_event, inputs), daemon=True).start()

if bc2.button("❌ 취소", disabled=not st.session_state.is_running):
    st.session_state.stop_event.set()
    st.session_state.is_running = False
    st.rerun() # 취소 시에도 즉각 반영

# 로그 영역
st.markdown("---")
st.markdown("**📝 실행 로그**")
log_container = st.container(height=300)

# 실시간 로그 업데이트 루프 (수정됨)
processed_finish = False
while True:
    try:
        msg = st.session_state.message_queue.get_nowait()
        if msg.startswith("🚨UI_LOG:"):
            st.session_state.log_messages.append(msg.replace("🚨UI_LOG:", ""))
        elif msg.startswith("🚨UI_ERROR:"):
            st.session_state.log_messages.append(f"❌ {msg.replace('🚨UI_ERROR:', '')}")
            st.session_state.is_running = False
            processed_finish = True
        elif msg.startswith("🚨UI_FINISH:"):
            # 종료 신호를 받으면 is_running을 해제하고 루프를 탈출하여 rerun 실행
            st.session_state.is_running = False
            processed_finish = True
    except queue.Empty:
        break

with log_container:
    for m in reversed(st.session_state.log_messages):
        color = "green" if "🎉" in m or "✅" in m else "red" if "❌" in m or "⚠️" in m else "black"
        st.markdown(f'<p style="font-size:12px; margin:0; color:{color}; font-family:monospace;">{m}</p>',
                    unsafe_allow_html=True)

# 작업 종료 신호를 받았다면 화면을 다시 그려 버튼을 활성화
if processed_finish:
    st.rerun()

if st.session_state.is_running:
    time.sleep(0.1)
    st.rerun()
