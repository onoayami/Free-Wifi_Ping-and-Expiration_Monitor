import rumps

class TestApp(rumps.App):
    @rumps.clicked("Parent", "Child1")
    def child1(self, _):
        print("Child1")

    @rumps.clicked("Parent", "Child2")
    def child2(self, _):
        print("Child2")

if __name__ == "__main__":
    print(TestApp("Test", menu=["Parent"]).menu)
