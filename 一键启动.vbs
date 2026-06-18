Option Explicit

Dim shell, fso, folder, appPath, logPath
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

folder = fso.GetParentFolderName(WScript.ScriptFullName)
appPath = fso.BuildPath(folder, "start_gui.pyw")
logPath = fso.BuildPath(folder, "launcher_debug.log")
shell.CurrentDirectory = folder

WriteLog "launcher start"

If Not fso.FileExists(appPath) Then
    Fail "Cannot find start_gui.pyw. Please keep it in the same folder as this launcher."
End If

If TryKnownCandidates() Then
    WScript.Quit 0
End If

Fail "No usable Python runtime was found. Please run: pip install -r requirements.txt"

Function TryKnownCandidates()
    Dim pathText, paths, i, p, candidate, userProfile, localAppData
    TryKnownCandidates = False

    pathText = shell.ExpandEnvironmentStrings("%PATH%")
    paths = Split(pathText, ";")
    For i = 0 To UBound(paths)
        p = Trim(paths(i))
        If p <> "" Then
            candidate = fso.BuildPath(p, "python.exe")
            If TryCandidate(candidate) Then
                TryKnownCandidates = True
                Exit Function
            End If
        End If
    Next

    userProfile = shell.ExpandEnvironmentStrings("%USERPROFILE%")
    localAppData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")

    If TryCandidate(fso.BuildPath(userProfile, "miniconda3\python.exe")) Then
        TryKnownCandidates = True
        Exit Function
    End If

    If TryCandidate("C:\Python314\python.exe") Then
        TryKnownCandidates = True
        Exit Function
    End If

    If TryCandidate(fso.BuildPath(localAppData, "Programs\Python\Python310\python.exe")) Then
        TryKnownCandidates = True
        Exit Function
    End If
End Function

Function TryCandidate(pyExe)
    Dim rc, pywExe
    TryCandidate = False
    If pyExe = "" Then Exit Function
    If Not fso.FileExists(pyExe) Then Exit Function

    WriteLog "test " & pyExe
    rc = shell.Run(Quote(pyExe) & " -c " & Quote("import tkinter; import playwright"), 0, True)
    WriteLog "result " & CStr(rc) & " " & pyExe
    If rc <> 0 Then Exit Function

    pywExe = fso.BuildPath(fso.GetParentFolderName(pyExe), "pythonw.exe")
    If fso.FileExists(pywExe) Then
        RunApp pywExe
    Else
        RunApp pyExe
    End If
    TryCandidate = True
End Function

Sub RunApp(exePath)
    Dim rc, windowStyle
    WriteLog "run " & exePath
    If LCase(fso.GetFileName(exePath)) = "pythonw.exe" Then
        windowStyle = 1
    Else
        windowStyle = 0
    End If
    On Error Resume Next
    Err.Clear
    rc = shell.Run(Quote(exePath) & " " & Quote(appPath), windowStyle, False)
    If Err.Number <> 0 Then
        Fail "Startup failed: " & Err.Description
    End If
    On Error GoTo 0
End Sub

Sub Fail(message)
    WriteLog "fail " & message
    MsgBox message & vbCrLf & vbCrLf & "Details: " & logPath, vbCritical, "Startup failed"
    WScript.Quit 1
End Sub

Sub WriteLog(message)
    Dim file
    On Error Resume Next
    Set file = fso.OpenTextFile(logPath, 8, True)
    file.WriteLine Now & " " & message
    file.Close
    On Error GoTo 0
End Sub

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
