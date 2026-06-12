import rumps
import urllib.request
from datetime import datetime, timedelta
import AppKit
import subprocess
import json
import os
import re
import threading

# Macの下のDock（メニューバー）に実行中のPythonアイコンを表示しないための設定
info = AppKit.NSBundle.mainBundle().infoDictionary()
info['LSUIElement'] = '1'
# macOSに通知を許可させるためのダミーのアプリ識別子（Bundle ID）を設定
info['CFBundleIdentifier'] = 'com.python.wifimonitor'

class WiFiMonitorApp(rumps.App):
    def __init__(self):
        # 最初はオフライン状態として起動
        super(WiFiMonitorApp, self).__init__("🌀")
        self.is_online = False
        self.connected_time = None
        self.notified_targets = set() # 通知済みの目標時間を記録するセット
        self.display_mode = "timer"  # 表示モード: "timer" (時間表示) または "ping" (応答速度)
        self.suppress_notif = False  # 通知をオフにするかのフラグ
        self.cached_ping_display = "" # Ping表示のキャッシュ
        # 連続で何回チェックに失敗したらオフライン確定とするか（瞬断の誤判定を防ぐ）
        self.failure_count = 0
        self.offline_threshold = 4  # 4回連続失敗（=約20秒）で初めてオフライン扱い
        self.target_minutes = [50, 55] # 設定された通知時間（複数可、デフォルト50分と55分）
        self.disconnected_time = None # オフラインになった時刻を記録
        
        # --- 安全なWi-Fi (自宅など) の設定保持 ---
        self.config_path = os.path.expanduser("~/.wifimonitor_safe_ssids.json")
        self.safe_ssids = self.load_safe_ssids()
        self.current_ssid = None
        self.is_safe_wifi = False

        # 最後にcheck_networkが実行された時刻（スリープ判定に使用）
        self.last_heartbeat = datetime.now()

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
            client = CoreWLAN.CWWiFiClient.sharedWiFiClient()
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

    @rumps.timer(5) # 5秒ごとに通信チェック
    def check_network(self, _):
        now = datetime.now()
        
        # 前回チェックからの経過時間を記録（スリープ判定に使用）
        # Macがスリープ中はこの関数自体が止まるため、30秒以上開いていればスリープと判断できる
        time_since_last_check = (now - self.last_heartbeat).total_seconds()
        self.last_heartbeat = now

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

        new_ssid = getattr(self, "get_current_ssid", lambda: None)()
        ssid_changed = getattr(self, "current_ssid", None) is not None and new_ssid is not None and getattr(self, "current_ssid", None) != new_ssid
        
        if self.display_mode == "ping" and current_status:
            self.cached_ping_display = self.get_ping_status()
        else:
            self.cached_ping_display = ""

        if current_status:
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
                                set dialogResult to display dialog "未登録のWi-Fi ({ssid_disp}) に接続しました。\\n何分後に通知タイマーをスタートしますか？\\n※複数設定はカンマ区切り\\n※キャンセルで通知をオフにします" default answer "50, 55" buttons {{"キャンセル", "スタート"}} default button "スタート" cancel button "キャンセル" with title "Wi-Fi Monitor" as informational
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

    @rumps.timer(1) # 1秒ごとに表示更新
    def update_display(self, _):
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
            
            if getattr(self, "is_safe_wifi", False):
                self.title = f"🏠 {time_str}" if self.display_mode == "timer" else self.cached_ping_display
            else:
                self.title = f"🌐 {time_str}" if self.display_mode == "timer" else self.cached_ping_display
                
            # 目標時間経過していて、まだ通知していなければ（かつ通知オフ設定でなければ）アラート
            if not self.suppress_notif and active_targets:
                for i, target in enumerate(active_targets):
                    if elapsed_minutes >= target and target not in self.notified_targets:
                        is_last_target = (i == len(active_targets) - 1)
                        
                        if is_last_target:
                            # 最後のアラートタイミングの場合はスライダーではなくポップアップを表示
                            title_text = f"Wi-Fi Monitor: ⚠️時間切れ直前 ({target}分経過)"
                            alert_script = f'''
                            tell application "System Events"
                                activate
                                set theResponse to button returned of (display alert "{title_text}" message "接続から{time_str}が経過しました。\\n\\n設定した通知時間({target}分)になりました。作業を保存してください！" buttons {{"OK", "Wi-Fi設定を開く"}} default button "OK" as warning)
                                if theResponse is "Wi-Fi設定を開く" then
                                    do shell script "open x-apple.systempreferences:com.apple.wifi-settings-extension"
                                end if
                            end tell
                            '''
                            subprocess.Popen(["osascript", "-e", alert_script])
                        else:
                            # 途中でのアラートはスライド通知のみ表示
                            title_text = f"Wi-Fi Monitor: ⚠️時間切れ間近 ({target}分経過)"
                            rumps.notification(
                                title=title_text,
                                subtitle=f"接続から{time_str}が経過しました",
                                message="必要に応じて、作業を保存しましょう！",
                                actionButton="Wi-Fi設定を開く",
                                data={"action": "open_wifi_from_alert"}
                            )
                        
                        self.notified_targets.add(target)
        else:
            # オフライン時の表示
            
            # オフラインになってから5分(300秒)以上経過したらカウントを完全にリセットする
            if self.disconnected_time and (datetime.now() - self.disconnected_time).total_seconds() > 300:
                self.connected_time = None
                self.disconnected_time = None
                self.notified_targets.clear()
                
            self.title = "🔴" if self.display_mode == "timer" else "🔴 オフライン"

    @rumps.clicked("🏠 タイマー非通知wifi設定")
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
            title="🏠 タイマー非通知wifiリスト",
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

    @rumps.clicked("⚙️ タイマーの設定")
    def manage_timer_settings(self, _):
        """ タイマー設定の分岐用UI """
        response = rumps.alert(
            title="⚙️ タイマーの設定",
            message="実行する操作を選択してください。",
            ok="通知時間の設定",
            cancel="キャンセル",
            other="時間カウントをリセットする"
        )
        if response == 1:
            self._open_timer_settings()
        elif response == -1:
            self._reset_timer()

    def _reset_timer(self):
        """ カウントを現在時刻から0分にリセットする """
        if self.is_online:
            # リセット前に確認のポップアップを出す
            response = rumps.alert(
                title="タイマーのリセット",
                message="接続タイマーを0分にリセットしますか？",
                ok="リセットする",
                cancel="キャンセル"
            )
            
            # responseが 1 (OK) の場合のみリセット処理を実行
            if response == 1:
                self.connected_time = datetime.now()
                self.notified_targets.clear()
                self.update_display(None)
                rumps.notification("Wi-Fi Monitor", "タイマーをリセットしました", "カウントを0分にリセットしました")
        else:
            rumps.alert("エラー", "Wi-Fiに接続されていないためリセットできません。")

    def _open_timer_settings(self):
        """ タイマーの時間を変更するポップアップウィンドウを表示 """
        # 現在の設定をカンマ区切りで表示準備
        current_setting_str = ", ".join(map(str, self.target_minutes)) if self.target_minutes else "0"
        
        # 現在のステータスをテキスト化
        if self.target_minutes:
            current_display = f"【現在の設定: {current_setting_str} 分後】"
        else:
            current_display = "【現在の設定: オフ(通知しない)】"
        
        window = rumps.Window(
            title="通知タイマーの設定",
            message=f"{current_display}\n\n接続開始から何分後に通知を出しますか？\n複数設定する場合はカンマで区切ってください（例: 50, 55）\n※ 0 を入力すると通知をオフにできます。",
            default_text=current_setting_str,
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
                
                # この時点で既に過ぎている時間は通知済みにする（再設定時に即アラートが出るのを防ぐため）
                if self.is_online and self.connected_time:
                    now = datetime.now()
                    elapsed_minutes = int((now - self.connected_time).total_seconds()) // 60
                    self.notified_targets = {m for m in self.target_minutes if elapsed_minutes >= m}
                else:
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

    @rumps.clicked("⏱️ 表示切替 (接続時間 ⇆ Ping)")
    def toggle_display_mode(self, _):
        """ タイマー表示とPing速度表示を切り替える """
        if self.display_mode == "timer":
            self.display_mode = "ping"
        else:
            self.display_mode = "timer"
            
            # Pingから接続時間表示に戻したときにポップアップで確認
            response = rumps.alert(
                title="Wi-Fi時間通知を行います",
                message="",
                ok="OK（続行）",
                cancel="通知しない"
            )
            
            # responseは 0 (cancel) または 1 (ok) で返る
            if response == 0:
                self.suppress_notif = True
            else:
                self.suppress_notif = False
                
        self.check_network(None)  # Ping状態を即時取得
        self.update_display(None) # 表示を即時更新

    @rumps.clicked("🛜 Wi-Fi設定を開く")
    def open_wifi_settings(self, _):
        """ メニューからMacの標準Wi-Fi設定画面を開く """
        try:
            # macOSのシステム設定（Wi-Fi画面）を呼び出すコマンド
            subprocess.run(["open", "x-apple.systempreferences:com.apple.wifi-settings-extension"])
        except Exception:
            pass

if __name__ == "__main__":
    print("🔄 WiFi Monitorを起動しています...（終了するには Ctrl+C を押してください）")
    app = WiFiMonitorApp()
    print("✅ メニューバーへの登録を試みました。Macの画面右上を確認してください。")
    app.run()