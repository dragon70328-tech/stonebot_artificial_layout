@echo off
REM Batch-dump REA managed-assembly evidence for the GstoneCAD (YouShi CAD) install.
REM Targets are byte-identical copies under .\targets. The original install path is
REM C:\Program Files (x86)\YouShiCAD and every record carries its own SHA-256.
setlocal
set REA=npx -y rea-agents@6.4.0
set OUT=%~dp0
for %%f in ("%~dp0targets\*.dll") do (
  echo === %%~nxf
  call %REA% inspect-managed-artifact "%%~ff" --format json > "%OUT%ident_%%~nf.json"
  call %REA% inspect-managed-members "%%~ff" --format json > "%OUT%members_%%~nf.json"
)
endlocal
