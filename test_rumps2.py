import rumps

class TestApp(rumps.App):
    @rumps.clicked("Parent", "Child1")
    def child1(self, _):
        print("Child1")

app = TestApp("Test")
print(app.menu.keys())
print(list(app.menu["Parent"].keys()))
