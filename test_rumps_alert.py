import rumps

class TestApp(rumps.App):
    @rumps.clicked("Test")
    def do_test(self, _):
        response = rumps.alert(
            title="Test",
            message="Test Msg",
            ok="現在のWi-Fiを登録",
            cancel="閉じる",
            other="過去のものから選んで削除…"
        )
        print("Response:", response)

if __name__ == "__main__":
    import threading, os, time
    def exit_app():
        os._exit(0)
    threading.Timer(5.0, exit_app).start()
    app = TestApp("Test")
    app.run()
