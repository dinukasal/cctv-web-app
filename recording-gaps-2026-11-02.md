# VMS is not recording continuously — investigation

Date: 2 November 2026
Machine: Windows 11 laptop `DESKTOP-XXXXXXX` (192.168.1.146 / 192.168.1.224)
Question asked: "the VMS software is not continuously recording — I checked the .104
folder, it's lacking recordings."

**Answer: correct, and it is much worse than the .104 folder alone suggests. Two of
the seven cameras are effectively unrecorded, and the cause is not the PC, the disk,
or the recording configuration.**

---

## 1. Headline finding

Over a 45.7-hour window (31 Oct 10:00 → 2 Nov 07:40):

| Camera | Segments | Missing | Format | Verdict |
| --- | --- | --- | --- | --- |
| .105 | 94 | none | .h265 | continuous |
| .106 | 93 | none | .h265 | continuous |
| .234 | 93 | none | .h265 | continuous |
| .103 | 61 | 18.0 h | .avi | flaky |
| .108 | 61 | 17.0 h | .avi | flaky |
| .104 | 7 | **44.0 h of 45.7** | .avi | effectively not recorded |
| .101 | 0 in window | dead since 29 Oct | .avi | locked account (separate fault) |

`.104` produced 7 segments where it should have produced roughly 90.

The three continuous cameras are exactly the XM SDK family (`Vendor=0`, the ones
VMS talks to over the SDK, recorded as raw HEVC). The three failing cameras are
exactly the `Vendor=1` family — the ones whose RTSP lives on port 10554 and which
VMS reaches on port 10080, recorded as AVI.

That is not a coincidence. It is a per-camera-family failure.

## 2. What .104's failure actually looks like

    16:09:43 → 17:09:44    51.6 MB    healthy 30-minute segment
    17:09:44 → 17:39:44     0.81 MB   a dead stub: 1.6% of a normal segment
    17:39:44 → 07:40:09     nothing for 14 hours

The stream did not merely stop. VMS held a package open for the full 30 minutes and
captured 810 KB of it, closed it, and then did not open another package until 07:40
the next morning.

For contrast, healthy segments on those cameras are 32–52 MB each, and on the HEVC
cameras 150–235 MB.

## 3. Causes ruled out, with the evidence

**Not the PC sleeping.** This was the obvious suspect and it is wrong:
`.105/.106/.234` recorded *continuously through every gap*. The machine was awake
and recording while those three produced nothing. Confirmed independently by the
power configuration — the laptop is on AC (`BatteryStatus 2`, 97%), where sleep is
disabled outright (`Sleep after` = 0x00000000 on AC).

**Not the recording configuration.** `config.ini` in
`C:\Program Files (x86)\VMS\data\users\admin\` says:

    [Record]
    IsCover=true        overwrite when the disk fills (cyclic recording)
    LeaveSpace=5000     keep 5 GB free
    PackageTime=30      30-minute segments
    Disks=D:/

Continuous recording to D:, exactly as intended. It is configured correctly and
fails anyway.

**Not disk space.** D: had 964 GB free at the time — a month of capacity at this
rate (~33 GB/day across six cameras).

**Not the cameras being offline.** All three stream fine. go2rtc holds verified
1080p H.264 streams from `.103/.104/.108` throughout, and their RTSP ports respond.

**Not VMS noticing and logging it.** It did not. All 196 rows in the alarm log
belong to `.234` and `.101`; there is not a single alarm entry for `.103`, `.104`
or `.108`. VMS never registered that it was losing them.

## 4. What the evidence points to

VMS's video session with that camera family dies and is not retried. It keeps
polling the devices (the operation log shows entries for `.104` at 15–30 minute
intervals right through the 14-hour hole), so it believes it is talking to the
camera — but no video arrives and no recording happens.

VMS's own log shows the recovery events, and they match when recording resumed:

    2026-11-01T16:09:43  .104  LogType=0 SubLog=5  info='Stream Type:Main Stream'
    2026-11-01T16:09:56  .103  LogType=0 SubLog=5  info='Stream Type:Main Stream'
    2026-11-01T16:09:57  .108  LogType=0 SubLog=5  info='Stream Type:Main Stream'
    ...
    2026-11-02T07:40:09  .104  LogType=0 SubLog=7
    2026-11-02T07:40:29  .108  LogType=0 SubLog=7
    2026-11-02T07:40:59  .103  LogType=0 SubLog=7

Those two timestamps — 1 Nov 16:09 and 2 Nov 07:40 — are exactly when the three
cameras started recording again, and both are moments when the machine was being
used. Opening the VMS live view, or the app re-initialising itself, appears to be
what re-establishes the sessions.

Beyond that I cannot see further from outside the application: the retry logic
lives inside closed-source VMS code. What is proven is the shape of the failure —
per camera family, silent, persistent for hours, and recovered only by an external
trigger.

**Currently writing again.** All three have a freshly opened package (created
07:40) held open under lock by VMS, so as of this morning they are recording.

## 5. Correction to an earlier statement

I previously said a grey tile in VMS was cosmetic, because the live-view decoder is
a separate pipeline from the recording engine — and I had measured file growth
continuing on cameras whose tiles were grey. That was true for the cameras I
actually measured, but wrong as a general rule: `.104` was grey *and* failing, and
its recording stopped at the same moment. The grey tile should have been correlated
against that specific camera before generalising.

A grey tile is therefore worth treating as a symptom to check, not as noise.

## 6. Recommended actions, in order

**1. Fix 192.168.1.101.** Its admin account is locked (`Ret 205`), so VMS has never
been able to record it; nothing since 29 Oct. Power-cycle the camera to clear the
lock, then reset the password via the phone app if needed, then update it in VMS's
Device Manager. It appears nowhere in VMS's logs at all — it is simply absent.

**2. Recover the three now.** Restart VMS. This forces fresh sessions, and it is
what has cleared the condition twice already.

**3. Stop the bleeding — bound the loss.** Restart VMS on a schedule (nightly, or
every couple of hours) so a dead session costs that interval rather than 14 hours.
Low risk and reversible. Needs a session-1 interactive task, since VMS is a GUI
application.

**4. The real fix — take VMS out of the path for those three.** go2rtc already
holds stable, verified streams for `.103/.104/.108` and reconnects far more
aggressively than VMS does. Two options:

   a. Point VMS at go2rtc's RTSP re-export instead of the vendor protocol:
      `rtsp://192.168.1.146:8554/cam103` (and `cam104`, `cam108`) — if VMS can add
      an RTSP or ONVIF source.
   b. Let ffmpeg record those three continuously as a parallel safety net driven by
      go2rtc's RTSP output, independent of VMS entirely.

**Caution while diagnosing:** go2rtc's streams and VMS's sessions compete for the
same cameras, and that family allows very few simultaneous sessions. Keep the
camera dashboard closed during diagnosis so a fault I introduced is not mistaken
for the pre-existing one.

## 7. Appendix — how this was measured

Per-camera gap analysis from the filenames (they embed their start time,
`YYYYMMDDHHMMSSmmm.avi`): parse the first 14 characters as a timestamp, sort, and
flag any start-to-start interval longer than 35 minutes (segments are 30 minutes).

Machine state was checked with Kernel-Power events (`Get-WinEvent`,
provider `Microsoft-Windows-Kernel-Power`) plus `powercfg /a` and the current AC/DC
sleep settings.

VMS's own state comes from SQLite databases in
`C:\Program Files (x86)\VMS\data\users\admin\`:

    devices.db      Devices table: Address, Port, UserName, Password, Vendor, LoginType
    storage.db      ManualRecord: the index of every clip written
    logs.db         operateLogInfo (operation/stream events), alarmLogInfo (alarms)
    recordplan.db   recording plan (0 rows — no per-camera schedule is in use)
    config.ini      PackageTime=30, IsCover=true, Disks=D:/

Device IDs used in the logs: 1=.101, 2=.103, 7=.105, 13=.106, 16=.234, 17=.104,
18=.108.

A file being held open by another process (VMS) can be detected by attempting to
open it with no sharing — `[System.IO.File]::Open(path,'Open','ReadWrite','None')`
— which is how the currently-open segment was identified.

## 8. Related

This is the second fault in the same recorder. The first — `.101`'s locked admin
account — is documented in `README.md` in this folder, along with the rest of the
camera setup.
