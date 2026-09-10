' stop_watch.vbs - stop a running intraday watch loop.
'
' Needed because the loop has no window, so there is no Ctrl-C to press.
'
' It matches exactly two shapes and terminates nothing else:
'   1. python.exe / pythonw.exe whose command line contains "hk_edge.py" AND
'      "--loop"          - started by hand, or by the old watch.bat
'   2. pythonw.exe whose command line contains "watch_silent.py"
'                       - started by the scheduled task HKWeatherEdge-Watch,
'                         or by watch_silent.vbs
' Shape 2 is not redundant: watch_silent.py is a launcher that imports hk_edge
' and calls main() itself, so its own command line carries neither
' "hk_edge.py" nor "--loop". Matching on those two alone would report "no
' running watch loop found" while the loop was in fact still running.

Option Explicit

Dim fso, svc, procs, p, n
Set fso = CreateObject("Scripting.FileSystemObject")
Set svc = GetObject("winmgmts:\\.\root\cimv2")
Set procs = svc.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process " _
                          & "WHERE Name = 'python.exe' OR Name = 'pythonw.exe'")

n = 0
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If (InStr(p.CommandLine, "hk_edge.py") > 0 _
            And InStr(p.CommandLine, "--loop") > 0) _
           Or InStr(p.CommandLine, "watch_silent.py") > 0 Then
            p.Terminate()
            n = n + 1
        End If
    End If
Next

' Terminate() is a hard kill, so watch_silent.py never reaches its cleanup and
' the lock file is left behind. That is harmless - watch_silent.py checks that
' the recorded pid is still alive and takes over a stale lock - but clear it.
On Error Resume Next
fso.DeleteFile fso.GetParentFolderName(WScript.ScriptFullName) & "\.watch_loop.pid", True
On Error GoTo 0

If n = 0 Then
    MsgBox "No running watch loop found.", 64, "hk-weather-edge"
Else
    MsgBox "Stopped " & n & " watch loop process(es).", 64, "hk-weather-edge"
End If
