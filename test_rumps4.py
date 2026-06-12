import rumps

class TestApp(rumps.App):
    @rumps.clicked("Parent", "Child1")
    def child1(self, _):
        pass

app = TestApp("Test")
print(app._menu.keys())

