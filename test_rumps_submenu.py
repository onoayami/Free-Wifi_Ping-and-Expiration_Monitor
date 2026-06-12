import rumps

class TestApp(rumps.App):
    def __init__(self):
        super(TestApp, self).__init__("Test")
        # Trying dict or list assignment
        self.menu = [
            rumps.MenuItem("Parent", callback=None)
        ]
        self.menu["Parent"].add(rumps.MenuItem("Child", callback=self.child))
        
    def child(self, _):
        print("Child")

app = TestApp()
print(app.menu.keys())
print(list(app.menu["Parent"].keys()))
