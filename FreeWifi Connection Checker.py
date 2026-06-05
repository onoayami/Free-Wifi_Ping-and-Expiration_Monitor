import rumps
import urllib.request
from datetime import datetime, timedelta
import AppKit
import subprocess
import json
import os

# Macの下のDock（メニューバー）に実行中のPythonアイコンを表示しないための設定
info = AppKit.NSBundle.mainBundle().infoDictionary()
info['LSUIElement'] = '1'

class WiFiMonitorApp(rumps.App):
    def __init__(self):
        # 最初はオフライン状態として起動
        super(WiFiMonitorApp, self).__init__("🔴")
        self.is_online = False
        self.connected_time = None
        self.notified_50min = False  # 50分経過の通知を出したかどうかのフラグ
        self.display_mode = "timer"  # 表示モード: "timer" (時間表示) または "ping" (応答速度)
        self.suppress_notif = False  # 50分タイマーの通知をオフにするかのフラグ

    def check_internet(self):
        """ 本当にインターネット(外部)に出られているかAppleの判定URLでチェック """
        try:
            # タイムアウトを3秒に設定。AppleのWi-Fiチェック用URLを利用
            response = urllib.request.urlopen('http://captive.apple.com/hotspot-detect.html', timeout=3)
            html = response.read().decode('utf-8')
            # Successが含まれていれば、Wi-Fiのログイン画面に邪魔されず正常に通信できている
            return "Success" in html
        except Exception:
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

    @rumps.timer(10) # 10秒ごとに実行
    def check_connection(self, _):
        now = datetime.now()

        # フリーWi-Fi等の場合は、ここでインターネット抜けできているかをチェック
        current_status = self.check_internet()
        
        ping_display = ""
        if self.display_mode == "ping" and current_status:
            ping_display = self.get_ping_status()

        if current_status:
            # 【オフライン -> オンライン】に切り替わった瞬間
            if not self.is_online:
                self.is_online = True
                # 検知のタイムラグを考慮し、安全のために「20秒前」に接続したことにしてタイマーを少し早める
                self.connected_time = now - timedelta(seconds=20)
                self.notified_50min = False
                
                time_str = self.connected_time.strftime("%H:%M")
                # デスクトップ通知でお知らせ
                rumps.notification(
                    title="Wi-Fi Monitor",
                    subtitle="インターネット接続を検知しました",
                    message=f"接続開始: {time_str} 〜監視を開始します"
                )
            
            # 【オンライン中】常に経過時間を計算してメニューバーの表示を更新
            if self.connected_time:
                elapsed_minutes = int((now - self.connected_time).total_seconds() / 60)
                
                # 50分以上経過している場合は警告表示、それ以外は通常表示
                if elapsed_minutes >= 50:
                    self.title = f"⚠️ {elapsed_minutes}分" if self.display_mode == "timer" else f"⚠️ {ping_display}"
                    
                    # 50分経過していて、まだ通知していなければ（かつ通知オフ設定でなければ）アラート
                    if not self.notified_50min and not self.suppress_notif:
                        rumps.notification(
                            title="Wi-Fi Monitor: ⚠️時間切れ間近",
                            subtitle=f"接続から{elapsed_minutes}分が経過しました",
                            message="まもなくフリーWi-Fiが切断される可能性があります。作業を保存してください！"
                        )
                        self.notified_50min = True
                else:
                    self.title = f"🌐 {elapsed_minutes}分" if self.display_mode == "timer" else ping_display

        else:
            # 【オンライン -> オフライン】に切り替わった瞬間
            if self.is_online:
                self.is_online = False
                
                # 接続していた時間を計算
                if self.connected_time:
                    elapsed_minutes = int((now - self.connected_time).total_seconds() / 60)
                    msg = f"通信が切断されました。（接続時間: 約{elapsed_minutes}分）"
                else:
                    msg = "通信が切断されました。再接続してください。"
                
                rumps.notification(
                    title="Wi-Fi Monitor: ❌切断",
                    subtitle="インターネット接続が失われました",
                    message=msg
                )
                
                # 表示をオフライン状態に戻す
                self.title = "🔴" if self.display_mode == "timer" else "🔴 オフライン"
                self.connected_time = None
                self.notified_50min = False

    @rumps.clicked("表示切替 (接続時間 ⇆ Ping)")
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
                
        self.check_connection(None)  # 表示を即時更新

    @rumps.clicked("Wi-Fi設定を開く")
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