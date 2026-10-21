import subprocess

result = subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq saplogon.exe"],
    capture_output=True,
    text=True
)

print(result.stdout)
