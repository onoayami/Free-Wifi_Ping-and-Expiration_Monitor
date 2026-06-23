import rumps
import urllib.request
from datetime import datetime, timedelta
from typing import Any, cast
import AppKit as _AppKit
import subprocess
import json
import os
import re
import threading

# PyObjCの動的属性(NSAlert等)を型チェッカーが認識できず誤検知を出すため、Any扱いにする
AppKit = cast(Any, _AppKit)

# Macの下のDock（メニューバー）に実行中のPythonアイコンを表示しないための設定
info = AppKit.NSBundle.mainBundle().infoDictionary()  # type: ignore[attr-defined]
info['LSUIElement'] = '1'
# macOSに通知を許可させるためのダミーのアプリ識別子（Bundle ID）を設定
info['CFBundleIdentifier'] = 'com.python.wifimonitor'

class WiFiMonitorApp(rumps.App):
    def __init__(self):
        # 最初はオフライン状態として起動
        super(WiFiMonitorApp, self).__init__("🌀")
        self.is_online = False
        self.first_check_done = False # 初回チェックが完了したかどうかのフラグ
        self.connected_time = None
        self.notified_targets = set() # 通知済みの目標時間を記録するセット
        self.display_mode = "timer"  # 表示モード: "timer" (時間表示) または "ping" (応答速度)
        self.suppress_notif = False  # 通知をオフにするかのフラグ
        self.cached_ping_display = "" # Ping表示のキャッシュ
        # 連続で何回チェックに失敗したらオフライン確定とするか（瞬断の誤判定を防ぐ）
        self.failure_count = 0
        self.offline_threshold = 4  # 4回連続失敗（=約20秒）で初めてオフライン扱い
        self.target_minutes = [50, 55] # 設定された通知時間（複数可、デフォルト50分と55分）
        self.previous_target_minutes = [50, 55] # ON/OFF切り替え時に復元するための保存用
        self.disconnected_time = None # オフラインになった時刻を記録
        
        # --- カスタムタイマー用 ---
        self.custom_timer_end_time = None
        self.custom_timer_name = ""
        self.custom_timer_duration_minutes = 0
        self.is_custom_timer_alert_showing = False
        
        # --- 安全なWi-Fi (自宅など) の設定保持 ---
        self.config_path = os.path.expanduser("~/.wifimonitor_safe_ssids.json")
        self.safe_ssids = self.load_safe_ssids()
        self.current_ssid = None
        self.is_safe_wifi = False

        # --- VPN機能の設定・状態保持 ---
        self.settings_path = os.path.expanduser("~/.wifimonitor_settings.json")
        self.vpn_feature_enabled = bool(self.load_settings().get("vpn_feature_enabled", False))
        self.vpn_connected = False  # 現在VPNに接続しているか
        self.vpn_name = None        # 接続中VPNの名前
        self._last_vpn_state = None # メニュー再描画最適化用キャッシュ

        # 最後にcheck_networkが実行された時刻（スリープ判定に使用）
        self.last_heartbeat = datetime.now()

        # --- メニューを望む順序で再構築 ---
        self.menu.clear()

        # 1. 表示切替（ネット速度／接続時間）— クリックで表示モードを切り替える
        self.display_toggle_menu = rumps.MenuItem("【表示内容】", callback=self.toggle_display_mode)
        self.menu.add(self.display_toggle_menu)
        
        self.countdown_display_menu = rumps.MenuItem("⏱️ タイマー残り時間", callback=self.show_countdown_mode)
        self.speed_menu = rumps.MenuItem("📶 ネット速度", callback=self.show_speed_mode)
        self.timer_display_menu = rumps.MenuItem("⏳ ネット接続時間", callback=self.show_timer_mode)
        
        self.menu.add(self.countdown_display_menu)
        self.menu.add(self.speed_menu)
        self.menu.add(self.timer_display_menu)
        
        self.countdown_display_menu._menuitem.setHidden_(True)

        self.menu.add(rumps.separator)

        self.alert_setting_header = rumps.MenuItem("【フリーWi-fi 接続切れアラーム】", callback=self.do_nothing)
        self.menu.add(self.alert_setting_header)

        # 2. SSID表示（クリックでWi-Fi設定を開く）
        self.ssid_menu = rumps.MenuItem("\U0001f6dc 現在の接続先: 取得中...", callback=self.open_wifi_settings)
        self.menu.add(self.ssid_menu)

        # 2.5 VPN接続状態表示（VPN機能ONの時のみ表示。アラーム設定の上に移動）
        self.vpn_status_menu = rumps.MenuItem("🔐 VPN接続：現在 取得中...", callback=self.open_vpn_settings)
        self.menu.add(self.vpn_status_menu)

        # 3. アラーム設定状態表示（クリックでON/OFFを切り替える）
        self.alarm_status_menu = rumps.MenuItem("⌛️ アラーム設定：現在 取得中...", callback=self.toggle_alarm)
        self.menu.add(self.alarm_status_menu)

        # 4. 非通知Wi-Fiの設定
        self.safe_wifi_menu = rumps.MenuItem("🏠 アラーム非通知wifiの設定", callback=self.manage_safe_wifi)
        self.menu.add(self.safe_wifi_menu)

        self.menu.add(rumps.separator)

        # 6. タイマーの設定（○分後の通知をポップアップで設定）
        self.timer_setting_menu = rumps.MenuItem("⏱️ タイマー機能", callback=self.open_timer_setting_popup)
        self.menu.add(self.timer_setting_menu)

        self.menu.add(rumps.separator)

        # 7. 設定（VPN機能の有効化など）— Quitの上に配置
        self.settings_menu = rumps.MenuItem("⚙️ その他")

        # 接続時間のリセットを設定メニュー内に移動（VPN設定の上）
        self.reset_timer_menu = rumps.MenuItem("🔄 ネット接続時間のリセット", callback=self._reset_timer)
        self.settings_menu.add(self.reset_timer_menu)

        self.vpn_settings_item = rumps.MenuItem("🔐 VPN設定", callback=self.open_settings)
        self.settings_menu.add(self.vpn_settings_item)
        self.menu.add(self.settings_menu)

        self.menu.add(rumps.separator)
        # この後ろに rumps が自動で Quit ボタンを追加する

        # VPN機能の有効/無効に応じてVPN表示欄の表示状態を初期化する
        self._update_vpn_menu_visibility()

    @staticmethod
    def escape_for_applescript(text):
        """ AppleScriptの文字列リテラルに安全に埋め込めるようエスケープする """
        if text is None:
            return ""
        # バックスラッシュを先にエスケープし、その後ダブルクォートをエスケープする
        return str(text).replace("\\", "\\\\").replace('"', '\\"')

    def load_safe_ssids(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def save_safe_ssids(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.safe_ssids, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_settings(self):
        """ アプリ設定(VPN機能のON/OFFなど)をJSONから読み込む """
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
        return {}

    def save_settings(self):
        """ アプリ設定をJSONに保存する """
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump({"vpn_feature_enabled": self.vpn_feature_enabled}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_wifi_device(self):
        """ Wi-Fiのインターフェース名(en0など)を動的に取得する。機種差に対応。 """
        # 一度取得したらキャッシュして使い回す
        cached = getattr(self, "_wifi_device", None)
        if cached:
            return cached
        try:
            result = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                # "Hardware Port: Wi-Fi" の次行 "Device: enX" を探す
                m = re.search(r"Hardware Port:\s*Wi-Fi\s*\n\s*Device:\s*(\S+)", result.stdout)
                if m:
                    self._wifi_device = m.group(1)
                    return self._wifi_device
        except Exception:
            pass
        # 取得できなければ一般的なen0をフォールバックとして使う
        return "en0"

    def get_current_ssid(self):
        """ 接続中Wi-FiのSSIDを取得する。
        macOS 14.4以降では ipconfig/networksetup が位置情報権限なしでは伏せ字(<redacted>)を
        返すため、権限不要で実値が得られる system_profiler を確実なフォールバックとして用いる。 """
        device = self.get_wifi_device()

        # 取得結果が伏せ字(redacted)や無効値でないか判定するヘルパー
        def _valid(s):
            if not s:
                return False
            low = s.strip().lower()
            if "redacted" in low or low in ("", "you are not associated with an airport network."):
                return False
            return True

        # 最優先手段: CoreWLAN (Appleの公式フレームワーク)。位置情報の許可がある環境では即座に取得できる。
        try:
            import CoreWLAN
            client = CoreWLAN.CWWiFiClient.sharedWiFiClient()  # type: ignore[attr-defined]
            interface = client.interface()
            if interface is not None:
                ssid = interface.ssid()
                if _valid(ssid):
                    return str(ssid)
        except Exception:
            pass

        # 主手段: ipconfig getsummary <device> の出力からSSID行を抽出
        try:
            result = subprocess.run(["ipconfig", "getsummary", device], capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                # 行頭の空白を許容しつつ、BSSIDではなくSSID行のみにマッチさせる
                m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", result.stdout, re.MULTILINE)
                if m and _valid(m.group(1)):
                    return m.group(1).strip()
        except Exception:
            pass

        # 確実なフォールバック: system_profiler は位置情報の許可が無くても実SSIDを返す（やや低速）。
        # "Current Network Information:" の次行にSSID名（末尾コロン付き）が出力される。
        try:
            result = subprocess.run(["system_profiler", "SPAirPortDataType"], capture_output=True, text=True, timeout=8)
            if result.returncode == 0:
                m = re.search(r"Current Network Information:\s*\n\s*(.+?):\s*\n", result.stdout)
                if m and _valid(m.group(1)):
                    return m.group(1).strip()
        except Exception:
            pass

        # 最後のフォールバック: 旧来の networksetup（位置情報権限がある環境では機能する）
        try:
            result = subprocess.run(["networksetup", "-getairportnetwork", device], capture_output=True, text=True, timeout=3)
            if result.returncode == 0 and "Current Wi-Fi Network:" in result.stdout:
                ssid = result.stdout.split("Current Wi-Fi Network:")[1].strip()
                if _valid(ssid):
                    return ssid
        except Exception:
            pass

        return None

    def check_internet(self):
        """ 本当にインターネット(外部)に出られているかAppleの判定URLでチェック """
        # 一時的な瞬断やサーバ側の遅延を吸収するため、複数エンドポイントを順に試す。
        # どれか1つでも成功すればオンラインとみなす。
        endpoints = [
            'http://captive.apple.com/hotspot-detect.html',
            'http://www.gstatic.com/generate_204',  # Googleの接続確認用（中身が空でステータス204）
        ]
        for url in endpoints:
            try:
                # タイムアウトを5秒に延長（混雑したフリーWi-Fiでの取りこぼしを防ぐ）
                response = urllib.request.urlopen(url, timeout=5)
                code = getattr(response, "status", response.getcode())
                html = response.read().decode('utf-8', errors='ignore')
                response.close()
                # Appleは"Success"を返す。gstaticはステータス204で本文が空。
                if "Success" in html or code == 204:
                    return True
            except Exception:
                continue
        return False

    def get_ping_status(self):
        """ Google Public DNS (8.8.8.8) にpingを打って応答速度と絵文字を含んだ文字列を返す """
        try:
            # macOS用ping: -c 1(1回), -t 1(1秒タイムアウト)
            result = subprocess.run(["ping", "-c", "1", "-t", "1", "8.8.8.8"], capture_output=True, text=True)
            if result.returncode == 0 and "time=" in result.stdout:
                time_str = result.stdout.split("time=")[1].split(" ms")[0]
                ping_ms = float(time_str)
                if ping_ms < 50:
                    return f"🔵 {ping_ms:.0f}ms"  # 早い (50ms未満)
                elif ping_ms < 150:
                    return f"🟡 {ping_ms:.0f}ms"  # まぁまぁ (50〜150ms)
                else:
                    return f"🟠 {ping_ms:.0f}ms"  # 遅い (150ms以上)
            return "🔴 Timeout"  # タイムアウト等で切断
        except Exception:
            return "🔴 Error"

    def get_vpn_status(self):
        """ VPNの接続状態を取得する。
        1. macOS標準 (scutil)
        2. OpenVPNなどのサードパーティ製アプリ (ifconfig の utun / tun インターフェース) """
        try:
            # 1. macOSの標準ネットワーク設定 (IKEv2, L2TPなど)
            result = subprocess.run(["scutil", "--nc", "list"], capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "(Connected)" in line:
                        m = re.search(r'"([^"]+)"', line)
                        return True, (m.group(1) if m else "システムVPN")
                        
            # 2. OpenVPN, Tunnelblick, Tailscale 等 (utun, tun インターフェース)
            # ifconfig の出力から、物理ネットワークとは別に作られるトンネル(utunX等)で、
            # 現在 UP かつ IPv4 が割り当てられているものを探す
            result2 = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3)
            if result2.returncode == 0:
                current_if = None
                is_up_running = False
                for line in result2.stdout.splitlines():
                    if line and not line[0].isspace():
                        # utun, tun, ipsec で始まるインターフェースか
                        m = re.match(r'^(utun\d+|tun\d+|ipsec\d+):', line)
                        if m:
                            current_if = m.group(1)
                            # "<UP," と "RUNNING" の両方が含まれているか
                            is_up_running = "UP" in line and "RUNNING" in line
                        else:
                            current_if = None
                            is_up_running = False
                    elif current_if and is_up_running:
                        if "inet " in line:
                            # inet (IPv4) アドレスが割り当てられていればVPN接続とみなす
                            # (ローカルループバック 127.x.x.x は除外)
                            m_ip = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', line)
                            if m_ip and not m_ip.group(1).startswith("127."):
                                return True, f"OpenVPN"
        except Exception:
            pass
        return False, None

    @rumps.timer(2)  # 2秒ごとにPing速度を取得（ping -c1のみの軽い処理）
    def update_ping(self, _):
        # Ping表示モードかつオンライン時のみ取得して負荷を最小化する
        if self.display_mode != "ping" or not getattr(self, "is_online", False):
            return
        if getattr(self, "checking_ping", False):
            return
        self.checking_ping = True
        # pingはmacOSの-tタイムアウトで最大1秒ブロックするため、UIを止めないよう別スレッドで実行
        threading.Thread(target=self._async_update_ping, daemon=True).start()

    def _async_update_ping(self):
        try:
            self.cached_ping_display = self.get_ping_status()
        except Exception:
            pass
        finally:
            self.checking_ping = False

    @rumps.timer(5) # 5秒ごとに通信チェック
    def check_network(self, _):
        if getattr(self, "checking_network", False):
            return
        self.checking_network = True

        now = datetime.now()
        
        # 前回チェックからの経過時間を記録（スリープ判定に使用）
        # Macがスリープ中はこの関数自体が止まるため、30秒以上開いていればスリープと判断できる
        time_since_last_check = (now - self.last_heartbeat).total_seconds()
        self.last_heartbeat = now

        # 通信チェックがメイン(UI)スレッドをブロックするのを防ぐため、別スレッドで処理
        threading.Thread(target=self._async_check_network, args=(now, time_since_last_check), daemon=True).start()

    def _async_check_network(self, now, time_since_last_check):
        try:
            self._async_check_network_impl(now, time_since_last_check)
        except Exception:
            pass
        finally:
            self.checking_network = False

    def _async_check_network_impl(self, now, time_since_last_check):
        # フリーWi-Fi等の場合は、ここでインターネット抜けできているかをチェック
        raw_status = self.check_internet()
        if raw_status:
            # 成功したら失敗カウントをリセット
            self.failure_count = 0
            current_status = True
        else:
            # 失敗したらカウントを増やす。ただし現在オンライン中で連続失敗が閾値未満なら、
            # 一時的な瞬断とみなしてオンラインを維持する（誤オフライン判定の防止）
            self.failure_count += 1
            if self.is_online and self.failure_count < self.offline_threshold:
                current_status = True
            else:
                current_status = False

        # Ping速度の取得は専用タイマー(update_ping/2秒間隔)が担当する。
        # ここではオフライン時や非Ping表示時にキャッシュをクリアするだけにする。
        if not (self.display_mode == "ping" and current_status):
            self.cached_ping_display = ""

        # VPN機能が有効な場合のみ、VPNの接続状態を取得する（無効時は負荷をかけない）
        if getattr(self, "vpn_feature_enabled", False):
            self.vpn_connected, self.vpn_name = self.get_vpn_status()

        if current_status:
            # SSIDの取得はオンライン時のみ行う（オフライン時は未使用のため省略して負荷を下げる）
            new_ssid = self.get_current_ssid()
            ssid_changed = self.current_ssid is not None and new_ssid is not None and self.current_ssid != new_ssid
            # 【オフライン -> オンライン】に切り替わった瞬間 または SSIDが変わった瞬間
            if not getattr(self, "is_online", False) or ssid_changed:
                self.is_online = True
                self.current_ssid = new_ssid
                is_safe = (self.current_ssid in getattr(self, "safe_ssids", [])) if self.current_ssid else False
                self.is_safe_wifi = is_safe
                
                # 前回切断時から5分(300秒)以内の復帰かどうかを確認
                offline_seconds = (now - self.disconnected_time).total_seconds() if self.disconnected_time else 9999
                is_within_5min = offline_seconds <= 300
                was_sleeping = time_since_last_check >= 30
                
                if is_safe:
                    self.target_minutes = []
                    self.connected_time = now - timedelta(seconds=20)
                    self.notified_targets.clear()
                    self.display_mode = "ping"  # 非通知Wi-Fiの場合は自動でPing表示にする
                    rumps.notification(
                        title="Wi-Fi Monitor",
                        subtitle=f"🏠 安全なWi-Fiに接続: {self.current_ssid}",
                        message="接続タイマーは動作しません。"
                    )
                elif not ssid_changed and is_within_5min and was_sleeping:
                    # 5分以内の離席（同一Wi-Fi）復帰 → タイマーを継続する
                    rumps.notification(
                        title="Wi-Fi Monitor",
                        subtitle="💤 スリープから復帰しました",
                        message="前回のタイマーを継続します"
                    )
                else:
                    # 新規接続・ネットワーク変更・5分以上経過 → リセットして通知設定ポップアップを出す
                    self.display_mode = "timer" # 通常のWi-Fiの場合はタイマー表示に戻す
                    self.connected_time = now - timedelta(seconds=20)
                    self.notified_targets.clear()
                    self.suppress_notif = False  # 新接続時は必ず通知を有効化する
                    
                    if was_sleeping and not ssid_changed:
                        time_str = self.connected_time.strftime("%H:%M")
                        rumps.notification(
                            title="Wi-Fi Monitor",
                            subtitle="💤 スリープから復帰しました（5分以上経過）",
                            message=f"接続開始: {time_str} 〜 タイマーをリセットしました"
                        )
                    else:
                        rumps.notification(
                            title="Wi-Fi Monitor",
                            subtitle="フリーWifiタイマーを起動しました",
                            message=f"接続先: {getattr(self, 'current_ssid', '不明')}"
                        )
                        
                        # ポップアップを非同期で表示する関数
                        def prompt_timer_setup():
                            ssid_disp = getattr(self, "current_ssid", None) or "新しいネットワーク"
                            ssid_disp = self.escape_for_applescript(ssid_disp)
                            osascript_cmd = f'''
                            tell application "System Events"
                                activate
                                set dialogResult to display dialog "未登録のWi-Fi ({ssid_disp}) に接続しました。\\n何分後に通知タイマーをスタートしますか？\\n※複数設定はカンマ区切り\\n※キャンセルで通知をオフにします" default answer "50, 55" buttons {{"キャンセル", "スタート"}} default button "スタート" cancel button "キャンセル" with title "Wi-Fi Monitor"
                                return text returned of dialogResult
                            end tell
                            '''
                            try:
                                result = subprocess.run(["osascript", "-e", osascript_cmd], capture_output=True, text=True)
                                if result.returncode == 0:
                                    text = result.stdout.strip().replace("、", ",")
                                    if text.strip() == "0":
                                        self.target_minutes = []
                                    else:
                                        try:
                                            new_minutes = [int(m.strip()) for m in text.split(",") if m.strip().isdigit()]
                                            if new_minutes:
                                                self.target_minutes = sorted(list(set(new_minutes)))
                                        except Exception:
                                            pass
                                else:
                                    self.target_minutes = []
                            except Exception:
                                pass

                        # タイマー処理をブロックしないように別スレッドでポップアップ表示
                        threading.Thread(target=prompt_timer_setup, daemon=True).start()
                    
                # 切断時刻の記録をクリア
                self.disconnected_time = None
        else:
            # 【オンライン -> オフライン】に切り替わった瞬間
            if self.is_online:
                self.is_online = False
                self.disconnected_time = now  # 切断した時刻を記録
                
                # 接続していた時間を計算
                if self.connected_time:
                    total_seconds = int((now - self.connected_time).total_seconds())
                    elapsed_minutes = total_seconds // 60
                    elapsed_seconds = total_seconds % 60
                    
                    if elapsed_minutes == 0:
                        msg = "通信が切断されました。\n※そのままMacをスリープした場合、5分以内の復帰で引き継ぎます。"
                    else:
                        msg = f"通信が切断されました。（接続時間: {elapsed_minutes}分{elapsed_seconds:02d}秒）\n※そのままMacをスリープした場合、5分以内の復帰で引き継ぎます。"
                else:
                    msg = "通信が切断されました。"
                
                rumps.notification(
                    title="Wi-Fi Monitor: ❌切断",
                    subtitle="インターネット接続が失われました",
                    message=msg
                )
                
                # ※ここでは self.connected_time は None にしません（5分間の保留期間に入るため）
                
        # 初回の通信チェックが完了したことを記録
        if not getattr(self, "first_check_done", False):
            self.first_check_done = True

    @rumps.timer(1) # 1秒ごとに表示更新
    def update_display(self, _):
        # メニューの一番上のSSID表示を更新（軽量化のため変更がある場合のみ）
        if getattr(self, "is_online", False):
            ssid_name = getattr(self, "current_ssid", None) or "取得中..."
            new_ssid_title = f"\U0001f6dc 現在の接続先: {ssid_name}"
        else:
            new_ssid_title = "\U0001f6dc 現在の接続：オフライン（クリックしてWifi設定を開く）"
                
        if self.ssid_menu.title != new_ssid_title:
            self.ssid_menu.title = new_ssid_title

        # カスタムタイマーの有効状態を先に確認（表示制御に使用）
        _custom_timer_active = (
            self.custom_timer_end_time is not None
            and not getattr(self, "is_custom_timer_alert_showing", False)
            and (self.custom_timer_end_time - datetime.now()).total_seconds() > 0
        )
        # タイマーが無効になっていて表示モードが "custom_timer" なら "timer" に戻す
        if not _custom_timer_active and self.display_mode == "custom_timer":
            self.display_mode = "timer"
        # カスタムタイマーのメニュー項目を表示・非表示（状態が変化した時のみObjC呼び出し）
        if getattr(self, "_last_countdown_hidden", None) != (not _custom_timer_active):
            self._last_countdown_hidden = not _custom_timer_active
            self.countdown_display_menu._menuitem.setHidden_(not _custom_timer_active)
        # タイマー残り時間の計算（バー表示に使用）
        _custom_rem_str = ""
        if _custom_timer_active and self.custom_timer_end_time is not None:
            _rem = (self.custom_timer_end_time - datetime.now()).total_seconds()
            _custom_rem_str = f"⏱️ {int(_rem // 60):02d}:{int(_rem % 60):02d}"

        # 表示モード切替メニューの「[表示中]」を更新（選択中の方にのみ表示）
        speed_title = "📶 ネット速度 [表示中]" if self.display_mode == "ping" else "📶 ネット速度"
        timer_title = "⏳ ネット接続時間 [表示中]" if self.display_mode == "timer" else "⏳ ネット接続時間"
        countdown_title = "⏱️ タイマー残り時間 [表示中]" if self.display_mode == "custom_timer" else "⏱️ タイマー残り時間"
        if self.speed_menu.title != speed_title:
            self.speed_menu.title = speed_title
        if self.timer_display_menu.title != timer_title:
            self.timer_display_menu.title = timer_title
        if self.countdown_display_menu.title != countdown_title:
            self.countdown_display_menu.title = countdown_title

        # アラーム設定状態メニューの更新（軽量化のため状態が変わった場合のみ再描画）
        alarm_state = (getattr(self, "is_safe_wifi", False), self.suppress_notif, tuple(self.target_minutes))
        if getattr(self, "_last_alarm_state", None) != alarm_state:
            self._last_alarm_state = alarm_state
            if alarm_state[0]:
                self.alarm_status_menu.title = "⌛️ アラーム設定：現在 OFF（非通知Wi-Fi）"
            elif alarm_state[1]:
                self.alarm_status_menu.title = "⌛️ アラーム設定：現在 OFF（通知無効化）"
            elif not alarm_state[2]:
                self.alarm_status_menu.title = "⌛️ アラーム設定：現在 OFF"
            else:
                targets_str = ", ".join(map(str, alarm_state[2]))
                self.alarm_status_menu.title = f"⌛️ アラーム設定：現在 ON（{targets_str}分後に通知）"

        # VPN接続状態メニューの更新（VPN機能ON時のみ・状態が変わった場合のみ再描画）
        if getattr(self, "vpn_feature_enabled", False):
            vpn_state = (getattr(self, "vpn_connected", False), getattr(self, "vpn_name", None))
            if getattr(self, "_last_vpn_state", None) != vpn_state:
                self._last_vpn_state = vpn_state
                if vpn_state[0]:
                    name_str = f"（接続中：{vpn_state[1]}）" if vpn_state[1] else ""
                    self.vpn_status_menu.title = f"🔐 VPN接続：現在 ON {name_str}"
                else:
                    self.vpn_status_menu.title = "🔓 VPN接続：現在 OFF（未接続）"

        # カスタムタイマーの処理
        if getattr(self, "is_custom_timer_alert_showing", False):
            pass  # アラート表示中はメニューの更新をスキップ
        elif self.custom_timer_end_time is not None:
            now = datetime.now()
            remaining = self.custom_timer_end_time - now
            if remaining.total_seconds() > 0:
                rem_minutes = int(remaining.total_seconds() // 60)
                rem_seconds = int(remaining.total_seconds() % 60)
                # メニュー名に残り時間を更新（毎秒）
                if getattr(self, "custom_timer_name", ""):
                    new_timer_menu_title = f"⏱️タイマー起動中（{self.custom_timer_name}）：タイマーの再設定"
                else:
                    new_timer_menu_title = "⏱️タイマー起動中：タイマーの再設定"
                
                if self.timer_setting_menu.title != new_timer_menu_title:
                    self.timer_setting_menu.title = new_timer_menu_title
            else:
                # タイマー終了処理開始
                self.is_custom_timer_alert_showing = True
                past_minutes = getattr(self, "custom_timer_duration_minutes", 0)
                self.custom_timer_end_time = None
                self.timer_setting_menu.title = "⏱️タイマーの設定"
                
                # モーダルポップアップの作成
                alert = AppKit.NSAlert.alloc().init()
                alert.setMessageText_("タイマー終了")
                alert.setInformativeText_(f"【{past_minutes}分】経過")
                alert.addButtonWithTitle_("OK")
                alert.addButtonWithTitle_("再度同じ時間で設定")
                
                AppKit.NSApp.activateIgnoringOtherApps_(True)
                response = alert.runModal()
                
                self.is_custom_timer_alert_showing = False
                if response == 1001:  # "再度同じ時間で設定"が押された場合 (2番目のボタン: 1001)
                    self.custom_timer_end_time = datetime.now() + timedelta(minutes=past_minutes)
                    self.display_mode = "custom_timer"  # カウントダウン表示に戻す
                else:
                    self.timer_setting_menu.title = "⏱️タイマーの設定"
        else:
            if self.timer_setting_menu.title != "⏱️タイマーの設定":
                self.timer_setting_menu.title = "⏱️タイマーの設定"

        # メニューバー表示は一旦 new_title に組み立て、最後に「前フレームから変化がある時」のみ書き換える
        new_title = self.title
        if self.is_online and self.connected_time:
            now = datetime.now()
            total_seconds = int((now - self.connected_time).total_seconds())
            elapsed_minutes = total_seconds // 60
            elapsed_seconds = total_seconds % 60
            
            # 表示用の文字列を作成（0分の時は「0分」だけ表示する）
            if elapsed_minutes == 0:
                time_str = "0分"
            else:
                time_str = f"{elapsed_minutes}分{elapsed_seconds:02d}秒"

            # 目標時間以上経過している場合は警告表示（タイマー表示時のみ）、それ以外は通常表示
            active_targets = sorted([m for m in self.target_minutes if m > 0])
            
            _icon = "🏠" if getattr(self, "is_safe_wifi", False) else "🌐"
            if self.display_mode == "timer":
                new_title = f"{_icon} {time_str}"
            elif self.display_mode == "custom_timer" and _custom_rem_str:
                new_title = _custom_rem_str
            else:
                new_title = self.cached_ping_display
                
            # 目標時間経過していて、まだ通知していなければ（かつ通知オフ設定でなければ）アラート
            if not self.suppress_notif and active_targets:
                final_target = active_targets[-1]
                # この更新タイミングで「新たに」到達した目標時間を集める。
                # 設定変更直後など、既に複数の目標を過ぎている場合はここが複数件になる。
                newly_passed = [t for t in active_targets if elapsed_minutes >= t and t not in self.notified_targets]
                if newly_passed:
                    # 到達した目標はすべて通知済みとして記録する。
                    # （複数を個別に通知するとmacOSが連投を取りこぼすため、まとめて1回だけ通知する）
                    for t in newly_passed:
                        self.notified_targets.add(t)

                    highest = max(newly_passed)
                    if highest >= final_target:
                        # 最終目標に到達 → スライドではなくポップアップで強く警告
                        title_text = f"Wi-Fi Monitor: ⚠️時間切れ直前 ({highest}分経過)"
                        alert_script = f'''
                        tell application "System Events"
                            activate
                            set theResponse to button returned of (display alert "{title_text}" message "接続から{time_str}が経過しました。\\n\\n設定した通知時間({highest}分)になりました。作業を保存してください！" buttons {{"OK", "Wi-Fi設定を開く"}} default button "OK" as warning)
                            if theResponse is "Wi-Fi設定を開く" then
                                do shell script "open x-apple.systempreferences:com.apple.wifi-settings-extension"
                            end if
                        end tell
                        '''
                        threading.Thread(target=lambda: subprocess.run(["osascript", "-e", alert_script]), daemon=True).start()
                    else:
                        # 途中の目標 → スライド通知（1件のみ）
                        title_text = f"Wi-Fi Monitor: ⚠️時間切れ間近 ({highest}分経過)"
                        self._safe_notification(
                            title=title_text,
                            subtitle=f"接続から{time_str}が経過しました",
                            message="必要に応じて、作業を保存しましょう！",
                            action="open_wifi_from_alert"
                        )
        else:
            # オフライン時の表示
            
            # オフラインになってから5分(300秒)以上経過したらカウントを完全にリセットする
            if self.disconnected_time and (datetime.now() - self.disconnected_time).total_seconds() > 300:
                self.connected_time = None
                self.disconnected_time = None
                self.notified_targets.clear()
                
            if not getattr(self, "first_check_done", False):
                if self.display_mode == "custom_timer" and _custom_rem_str:
                    new_title = _custom_rem_str
                elif self.display_mode == "timer":
                    new_title = "🌀"
                else:
                    new_title = "🌀 起動中..."
            else:
                if self.display_mode == "custom_timer" and _custom_rem_str:
                    new_title = _custom_rem_str
                elif self.display_mode == "timer":
                    new_title = "🔴"
                else:
                    new_title = "🔴 オフライン"

        # 前フレームから表示内容が変わった時だけメニューバーを書き換える（不要な再描画を避ける）
        if self.title != new_title:
            self.title = new_title

    def _safe_notification(self, title, subtitle, message, action=None):
        """ スライド通知を確実に表示する。
        rumps.notification が失敗した場合は osascript の display notification で再試行する。 """
        try:
            kwargs = {"title": title, "subtitle": subtitle, "message": message}
            if action:
                kwargs["actionButton"] = "Wi-Fi設定を開く"
                kwargs["data"] = {"action": action}
            rumps.notification(**kwargs)  # type: ignore[arg-type]
            return
        except Exception:
            pass
        # フォールバック: osascript の display notification（rumps が使えない/失敗した場合）
        try:
            t = self.escape_for_applescript(title)
            s = self.escape_for_applescript(subtitle)
            m = self.escape_for_applescript(message)
            script = f'display notification "{m}" with title "{t}" subtitle "{s}"'
            threading.Thread(target=lambda: subprocess.run(["osascript", "-e", script]), daemon=True).start()
        except Exception:
            pass

    def manage_safe_wifi(self, _):
        """ 非通知（タイマー対象外）Wi-Fiの一覧表示・登録・解除をまとめて行うポップアップ """
        safe_list = getattr(self, "safe_ssids", []) or []
        current = getattr(self, "current_ssid", None)

        # 登録済みリストを1行ずつ見やすく整形
        if safe_list:
            list_text = "\n".join(safe_list)
        else:
            list_text = "（まだ登録されていません）"

        current_text = current if current else "取得できませんでした"

        message = (
            "以下に登録したWi-Fiでは\n接続タイマー・通知が作動しません。\n\n自宅や職場のWi-Fiを登録しておくのがおすすめです。\n\n"
            "＝＝＝ 登録済みリスト ＝＝＝\n"
            f"{list_text}\n\n"
            "――――――――――――\n"
            f"現在接続中: {current_text}"
        )

        # ok=登録 / other=解除 / cancel=閉じる の3ボタン
        response = rumps.alert(
            title="タイマー非通知wifiリスト",
            message=message,
            ok="現在のWi-Fiを非通知リストに登録",
            cancel="閉じる",
            other="過去のものから選んで削除"
        )

        if response == 1:
            # 現在のWi-Fiを登録
            self._register_current_safe_wifi()
        elif response == -1:
            # 登録を解除（どのWi-Fiを解除するか選ばせる）
            self._unregister_safe_wifi()

    def _register_current_safe_wifi(self):
        """ 現在接続中のWi-Fiを非通知リストに登録する """
        if not getattr(self, "is_online", False) or not getattr(self, "current_ssid", None):
            rumps.alert("エラー", "現在Wi-Fiのネットワーク名(SSID)が取得できない状態です。")
            return

        if self.current_ssid not in getattr(self, "safe_ssids", []):
            self.safe_ssids.append(self.current_ssid)
            self.save_safe_ssids()
            self.is_safe_wifi = True
            self.target_minutes = []
            self.display_mode = "ping"  # 登録時に自動でPing表示に切り替える
            self.update_display(None)
            rumps.alert("登録完了", f"「{self.current_ssid}」を非通知リストに登録しました。\n以降このネットワークではタイマーは起動しません。")
        else:
            rumps.alert("確認", f"「{self.current_ssid}」は既に登録されています。")

    def _unregister_safe_wifi(self):
        """ 登録済みの非通知Wi-Fiから解除する対象を選ばせて解除する """
        safe_list = getattr(self, "safe_ssids", []) or []
        if not safe_list:
            rumps.alert("確認", "非通知リストに登録されているWi-Fiはありません。")
            return

        # choose from list 用に各SSIDをエスケープしてリテラル化（先頭に全解除の選択肢を追加）
        ALL_OPTION = "【すべて解除】"
        items = [ALL_OPTION] + safe_list
        items_literal = ", ".join(f'"{self.escape_for_applescript(s)}"' for s in items)

        osascript_cmd = f'''
        tell application "System Events"
            activate
            set chosen to choose from list {{{items_literal}}} with title "非通知wifiリストの解除" with prompt "解除するWi-Fiを選択してください（複数選択可）" with multiple selections allowed
            if chosen is false then
                return "__CANCELLED__"
            end if
            set AppleScript's text item delimiters to linefeed
            return chosen as text
        end tell
        '''
        try:
            result = subprocess.run(["osascript", "-e", osascript_cmd], capture_output=True, text=True)
        except Exception:
            rumps.alert("エラー", "選択ダイアログの表示に失敗しました。")
            return

        if result.returncode != 0:
            return  # キャンセル等

        raw = result.stdout.strip()
        if not raw or raw == "__CANCELLED__":
            return

        # 改行区切りで返ってくる選択結果をパース
        selected = [line.strip() for line in raw.split("\n") if line.strip()]

        if ALL_OPTION in selected:
            removed_count = len(self.safe_ssids)
            self.safe_ssids = []
        else:
            before = len(self.safe_ssids)
            self.safe_ssids = [s for s in self.safe_ssids if s not in selected]
            removed_count = before - len(self.safe_ssids)

        self.save_safe_ssids()
        # 現在のWi-Fiが解除されたら通知対象に戻す
        if getattr(self, "current_ssid", None) not in self.safe_ssids:
            self.is_safe_wifi = False
        self.update_display(None)
        rumps.notification("Wi-Fi Monitor", "解除完了", f"{removed_count} 件のWi-Fiを非通知リストから解除しました。")

    def toggle_alarm(self, _):
        """ アラーム設定メニュー：ON/OFFの切り替えと設定変更 """
        if getattr(self, "is_safe_wifi", False):
            rumps.alert("アラーム設定", "現在は非通知Wi-Fiに接続中のため、\nアラームは自動でOFFになっています。")
            return

        if not self.target_minutes:
            # --- 現在OFF → ONにするか確認 ---
            response = rumps.alert(
                title="⌛️ アラーム設定",
                message="通知タイマーをONにしますか？",
                ok="はい",
                cancel="いいえ"
            )
            if response != 1:
                return

            # ONにする（前回の設定を復元）
            self.target_minutes = self.previous_target_minutes.copy() if getattr(self, "previous_target_minutes", []) else [50, 55]
            self.suppress_notif = False
            self.notified_targets.clear()
            self._last_alarm_state = None
            self.update_display(None)

            # 現在の設定を確認するポップアップ
            targets_str = ", ".join(map(str, self.target_minutes))
            response2 = rumps.alert(
                title="✅ タイマーをONにしました",
                message=f"現在の通知タイミング：接続から {targets_str} 分後",
                ok="OK",
                other="設定を変更する →"
            )
            if response2 == -1:
                self._open_timer_settings()

        else:
            # --- 現在ON → 設定確認ポップアップ ---
            targets_str = ", ".join(map(str, self.target_minutes))
            response = rumps.alert(
                title="⌛️ アラーム設定（現在 ON）",
                message=f"現在の通知タイミング：接続から {targets_str} 分後",
                ok="OK",
                cancel="OFFにする",
                other="設定を変更する →"
            )
            if response == -1:
                self._open_timer_settings()
            elif response == 0:
                # OFFにする
                self.previous_target_minutes = self.target_minutes.copy()
                self.target_minutes = []
                self.suppress_notif = True
                self._last_alarm_state = None
                self.update_display(None)
                rumps.notification("Wi-Fi Monitor", "アラーム OFF", "通知タイマーをオフにしました")

    def _reset_timer(self, _=None):
        """ 接続時間を現在時刻から0分にリセットする """
        if not self.is_online:
            rumps.alert("エラー", "Wi-Fiに接続されていないためリセットできません。")
            return

        # Step 1: リセット確認
        response = rumps.alert(
            title="接続時間のリセット",
            message="接続時間をリセットしますか？",
            ok="はい",
            cancel="いいえ"
        )
        if response != 1:
            return

        # リセット実行
        self.connected_time = datetime.now()
        self.notified_targets.clear()
        self._last_alarm_state = None
        self.update_display(None)

        # Step 2: アラームをONにするか確認
        response2 = rumps.alert(
            title="接続時間のリセット完了",
            message="再度、フリーwifiアラートをONにしますか？",
            ok="はい",
            cancel="いいえ"
        )
        if response2 == 1:
            self.target_minutes = self.previous_target_minutes.copy() if getattr(self, "previous_target_minutes", []) else [50, 55]
            self.suppress_notif = False
            self.notified_targets.clear()
            self._last_alarm_state = None
            self.update_display(None)
            targets_str = ", ".join(map(str, self.target_minutes))
            rumps.notification("Wi-Fi Monitor", "アラーム ON", f"接続から{targets_str}分後に通知します")
        else:
            rumps.notification("Wi-Fi Monitor", "接続時間をリセットしました", "カウントを0分にリセットしました")

    def _open_timer_settings(self):
        """ タイマーの時間を変更するポップアップウィンドウを表示 """
        # 現在のステータスと入力欄のデフォルト値を準備
        if self.target_minutes:
            current_setting_str = ", ".join(map(str, self.target_minutes))
            current_display = f"【現在の設定: {current_setting_str} 分後】"
            default_input = current_setting_str
        else:
            current_display = "【タイマーが設定されていません】"
            default_input = "50, 55" # タイマーがオフの時はデフォルトで50, 55を入れる
        
        window = rumps.Window(
            title="通知タイマーの設定",
            message=f"{current_display}\n\n接続開始から何分後に通知を出しますか？\n複数設定する場合はカンマで区切ってください（例: 50, 55）\n※ 0 を入力すると通知をオフにできます。",
            default_text=default_input,
            dimensions=(200, 20),
            ok="設定する",
            cancel="キャンセル"
        )
        response = window.run()
        
        # OKボタンが押された場合のみ処理
        if response.clicked:
            text = response.text.replace("、", ",") # 全角カンマ対応
            if text.strip() == "0":
                self.target_minutes = []
                rumps.notification("Wi-Fi Monitor", "タイマーをオフにしました", "時間経過の通知はおこないません")
                self.notified_targets.clear()
                self.update_display(None)
                return
                
            try:
                # 入力された文字列をカンマで分割し、数値のリストに変換
                new_minutes = [int(m.strip()) for m in text.split(",") if m.strip().isdigit()]
                
                if not new_minutes:
                    raise ValueError
                
                # 重複を省き、昇順に並べる
                self.target_minutes = sorted(list(set(new_minutes)))
                self.previous_target_minutes = self.target_minutes.copy()
                
                # 「50分後と55分後」のような表示用文字列を作成
                # リストが1つの場合は「50分後」、複数の場合は「50分後と55分後」になるように調整
                if len(self.target_minutes) == 1:
                    setting_display = f"{self.target_minutes[0]}分後"
                else:
                    mapped = [f"{m}分後" for m in self.target_minutes]
                    setting_display = "と".join(mapped)
                    
                # スライド通知から、画面中央に表示されるポップアップダイアログに変更
                rumps.alert(
                    title="タイマー設定完了",
                    message=f"反映されました！\n接続から{setting_display}に通知します。"
                )
                
                # タイマーを再設定した場合は通知フラグを確実にオフ（通知する）にし、
                # 既に時間を過ぎている設定値があればすぐアラートが出るよう対象をクリアする
                self.suppress_notif = False
                self.notified_targets.clear()
                
                self.update_display(None)
            except ValueError:
                rumps.alert("エラー", "半角数字（複数設定の場合はカンマ区切り）で入力してください。")

    @rumps.notifications
    def on_notification_click(self, data):
        """ 右上のスライドポップアップ（通知）をクリックしたときの処理 """
        if isinstance(data, dict):
            action = data.get("action")
            if action == "open_timer_setting":
                self._open_timer_settings()
            elif action == "open_wifi_from_alert":
                self.open_wifi_settings(None)

    def show_speed_mode(self, _):
        """ メニューバー表示を「ネット速度(Ping)」に切り替える """
        self.display_mode = "ping"
        self._last_alarm_state = None  # キャッシュをリセットして強制的に再描画させる
        self.update_ping(None)  # Ping速度を即時取得（2秒タイマーを待たずに反映）
        self.update_display(None) # 表示を即時更新

    def show_timer_mode(self, _):
        """ メニューバー表示を「ネット接続時間」に切り替える """
        self.display_mode = "timer"
        self._last_alarm_state = None  # キャッシュをリセットして強制的に再描画させる
        self.update_display(None) # 表示を即時更新

    def show_countdown_mode(self, _):
        """ メニューバー表示を「タイマー残り時間」に切り替える """
        self.display_mode = "custom_timer"
        self._last_alarm_state = None
        self.update_display(None)

    def toggle_display_mode(self, _):
        """ 「【表示内容】」クリックで表示モードを切り替える """
        timer_available = (
            getattr(self, "custom_timer_end_time", None) is not None
            and not getattr(self, "is_custom_timer_alert_showing", False)
        )
        if self.display_mode == "timer":
            self.show_speed_mode(None)
        elif self.display_mode == "ping":
            if timer_available:
                self.show_countdown_mode(None)
            else:
                self.show_timer_mode(None)
        else:  # custom_timer
            self.show_timer_mode(None)

    def do_nothing(self, _):
        """ 単なるテキスト表示用メニューアイテムのダミーコールバック """
        pass

    def open_timer_setting_popup(self, _):
        """ 「タイマーの設定」メニュー: 独立したタイマー機能として動作 """
        # タイマーが起動中の場合は再設定ポップアップを表示
        _timer_active = (
            self.custom_timer_end_time is not None
            and not getattr(self, "is_custom_timer_alert_showing", False)
            and (self.custom_timer_end_time - datetime.now()).total_seconds() > 0
        )
        if _timer_active:
            if self.custom_timer_name:
                alert_title = f"「{self.custom_timer_name}」タイマー起動中"
            else:
                alert_title = "タイマー起動中"
            timer_alert = AppKit.NSAlert.alloc().init()
            timer_alert.setMessageText_(alert_title)
            timer_alert.setInformativeText_("タイマーをどうしますか？")
            timer_alert.addButtonWithTitle_("タイマーをかけ直す")
            timer_alert.addButtonWithTitle_("タイマーの時間を再設定する")
            timer_alert.addButtonWithTitle_("タイマーをオフにする")
            timer_alert.addButtonWithTitle_("キャンセル")
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            response = timer_alert.runModal()
            if response == 1000:  # タイマーをかけ直す（同じ時間で再スタート）
                self.custom_timer_end_time = datetime.now() + timedelta(minutes=self.custom_timer_duration_minutes)
                self.display_mode = "custom_timer"
                return
            elif response == 1002:  # タイマーをオフにする
                self.custom_timer_end_time = None
                self.custom_timer_name = ""
                self.custom_timer_duration_minutes = 0
                self.timer_setting_menu.title = "⏱️タイマーの設定"
                return
            elif response == 1003:  # キャンセル
                return
            # response == 1001: タイマーの時間を再設定する → そのまま下の設定ダイアログへ

        # PyObjC (AppKit) を使って複数の入力欄を持つカスタムダイアログを作成
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("タイマーを設定する")
        alert.addButtonWithTitle_("設定する")
        alert.addButtonWithTitle_("キャンセル")

        # カスタムビュー（入力欄が入るコンテナ）
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 300, 70))

        # 1行目：名目（任意）
        name_label = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 40, 140, 20))
        name_label.setStringValue_("タイマー名目（任意）")
        name_label.setBezeled_(False)
        name_label.setDrawsBackground_(False)
        name_label.setEditable_(False)
        name_label.setSelectable_(False)
        view.addSubview_(name_label)

        name_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(140, 40, 150, 20))
        name_field.setPlaceholderString_("例：ポモドーロ")
        view.addSubview_(name_field)

        # 2行目：設定時間（分）
        time_label = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 10, 140, 20))
        time_label.setStringValue_("タイマー設定")
        time_label.setBezeled_(False)
        time_label.setDrawsBackground_(False)
        time_label.setEditable_(False)
        time_label.setSelectable_(False)
        view.addSubview_(time_label)

        time_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(140, 10, 60, 20))
        time_field.setPlaceholderString_("15")
        view.addSubview_(time_field)

        min_label = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(205, 10, 30, 20))
        min_label.setStringValue_("分")
        min_label.setBezeled_(False)
        min_label.setDrawsBackground_(False)
        min_label.setEditable_(False)
        min_label.setSelectable_(False)
        view.addSubview_(min_label)

        alert.setAccessoryView_(view)
        
        # ウィンドウが他のアプリの裏に隠れないように手前に持ってくる
        AppKit.NSApp.activateIgnoringOtherApps_(True)

        response = alert.runModal()

        if response == 1000:  # 設定する (1つめのボタン: 1000は NSAlertFirstButtonReturn)
            timer_name = name_field.stringValue()
            timer_time = time_field.stringValue()
            
            if not timer_time.isdigit():
                rumps.alert("エラー", "タイマー設定には半角数字で何「分」の形で入力してください。")
                return

            timer_minutes = int(timer_time)
            if timer_minutes <= 0:
                # 0が入力された場合はタイマーをリセット
                self.custom_timer_end_time = None
                self.custom_timer_name = ""
                self.custom_timer_duration_minutes = 0
                self.timer_setting_menu.title = "⏱️タイマーの設定"
                rumps.alert("タイマー解除", "タイマー設定を解除しました。")
                return

            # タイマー終了時刻を計算して保持
            self.custom_timer_duration_minutes = timer_minutes
            self.custom_timer_end_time = datetime.now() + timedelta(minutes=timer_minutes)
            self.custom_timer_name = timer_name.strip() if timer_name else ""
            self.display_mode = "custom_timer"  # タイマー設定直後にカウントダウン表示へ切り替え
            
            print(f"タイマー設定: 名目 '{self.custom_timer_name}', 時間 {timer_minutes}分")
            # 設定完了ポップアップは出さず、次の1秒更新でメニューが「⏱️ mm:ss」表記に変わるのに任せる

    def open_wifi_settings(self, _):
        """ メニューからMacの標準Wi-Fi設定画面を開く """
        try:
            # macOSのシステム設定（Wi-Fi画面）を呼び出すコマンド
            subprocess.run(["open", "x-apple.systempreferences:com.apple.wifi-settings-extension"])
        except Exception:
            pass

    def _update_vpn_menu_visibility(self):
        """ VPN機能のON/OFFに応じて「VPN接続」表示欄の表示・非表示を切り替える """
        enabled = getattr(self, "vpn_feature_enabled", False)
        try:
            self.vpn_status_menu._menuitem.setHidden_(not enabled)
        except Exception:
            pass
        if not enabled:
            # OFFにしたら状態をリセットして次回ON時に再取得させる
            self.vpn_connected = False
            self.vpn_name = None
            self._last_vpn_state = None

    def open_settings(self, _):
        """ 「⚙️ 設定」メニュー：VPN機能の追加をON/OFFする """
        current = "ON" if getattr(self, "vpn_feature_enabled", False) else "OFF"
        response = rumps.alert(
            title="⚙️ 設定",
            message=(
                f"【VPN機能の追加：現在 {current}】\n\n"
                "ONにすると、メニューの「アラーム設定」の下に\n"
                "「VPN接続」の状態表示欄が追加され、\n"
                "VPNに接続中かどうかが一目で分かります。"
            ),
            ok="ON にする",
            cancel="閉じる",
            other="OFF にする"
        )

        if response == 1:
            # VPN機能をONにする
            if not self.vpn_feature_enabled:
                self.vpn_feature_enabled = True
                self.save_settings()
                self._update_vpn_menu_visibility()
                # 即時にVPN状態を取得して表示へ反映する
                try:
                    self.vpn_connected, self.vpn_name = self.get_vpn_status()
                except Exception:
                    pass
                self._last_vpn_state = None
                self.update_display(None)
            rumps.notification("Wi-Fi Monitor", "VPN機能をONにしました", "メニューに「VPN接続」欄を追加しました")
        elif response == -1:
            # VPN機能をOFFにする
            if self.vpn_feature_enabled:
                self.vpn_feature_enabled = False
                self.save_settings()
                self._update_vpn_menu_visibility()
            rumps.notification("Wi-Fi Monitor", "VPN機能をOFFにしました", "「VPN接続」欄を非表示にしました")

    def open_vpn_settings(self, _):
        """ 「VPN接続」欄クリックでMacのVPN/ネットワーク設定画面を開く """
        try:
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preferences.network"])
        except Exception:
            pass

if __name__ == "__main__":
    print("🔄 WiFi Monitorを起動しています...（終了するには Ctrl+C を押してください）")
    app = WiFiMonitorApp()
    print("✅ メニューバーへの登録を試みました。Macの画面右上を確認してください。")
    app.run()