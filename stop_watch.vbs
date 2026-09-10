' stop_watch.vbs - stop a watch_silent.vbs loop.
'
' Needed because the loop window is hidden, so there is no Ctrl-C to press.
' Matches python.exe processes whose command line contains "hk_edge.py" and
' "--loop", and terminates exactly those. Nothing else is touched.

Option Explicit

Dim svc, procs, p, n
Set svc = GetObject("winmgmts:\\.\root\cimv2")
Set procs = svc.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process " _
                          & "WHERE Name = 'python.exe' OR Name = 'pythonw.exe'")

n = 0
For Each p In procs
    If Not IsNull(p.CommandLine) Then
        If InStr(p.CommandLine, "hk_edge.py") > 0 _
           And InStr(p.CommandLine, "--loop") > 0 Then
            p.Terminate()
            n = n + 1
        End If
    End If
Next

If n = 0 Then
    MsgBox "No running watch loop found.", 64, "hk-weather-edge"
Else
    MsgBox "Stopped " & n & " watch loop process(es).", 64, "hk-weather-edge"
End If
