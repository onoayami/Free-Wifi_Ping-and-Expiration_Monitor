import subprocess
osascript_cmd = '''
tell application "System Events"
    activate
    set chosen to choose from list {"A", "B"} with title "Title" with prompt "Prompt" with multiple selections allowed
    if chosen is false then return "__CANCELLED__"
    return chosen as text
end tell
'''
res = subprocess.run(["osascript", "-e", osascript_cmd], capture_output=True, text=True)
print("OUT:", res.stdout)
print("ERR:", res.stderr)
