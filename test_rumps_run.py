import rumps
import threading
import os

class TestApp(rumps.App):
    @rumps.clicked("🏠 タイマー非通知設定", "非通知wifiリスト")
    def action1(self, _):
        pass

    @rumps.clicked("🏠 タイマー非通知設定", "非通知wifiの登録解除")
    def action2(self, _):
        pass

def exit_app():
    os._exit(0)

if __name__ == "__main__":
    import time
    threading.Timer(2.0, exit_app).start()
    TestApp("Test").run()
