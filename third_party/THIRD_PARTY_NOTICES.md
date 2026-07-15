# Third-Party Notices

AI Landscape includes or downloads the following components:

## OpenCV YuNet

- Model: `face_detection_yunet_2023mar.onnx`
- Upstream: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
- License: MIT, see `licenses/YUNET-MIT.txt`
- SHA256: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`

## Source Han Fonts

- Source Han Sans SC Heavy 2.005R
- Source Han Serif SC Heavy 2.003R
- Upstream: https://github.com/adobe-fonts/source-han-sans and https://github.com/adobe-fonts/source-han-serif
- License: SIL Open Font License 1.1, see `licenses/SOURCE-HAN-OFL-1.1.txt`
- Font binaries are downloaded by `scripts/fetch_third_party.ps1` and are not stored in Git.

## FFmpeg

- Windows binary distribution: FFmpeg 8.1.2 essentials build by Gyan Doshi
- Upstream source: https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz
- Build is GPLv3 because it enables GPL components including libx264.
- License: see `licenses/FFMPEG-GPL-3.0.txt`
- Exact URLs, hashes and build configuration are recorded in `FFMPEG-BUILD.md`.
- Release artifacts include the corresponding unmodified FFmpeg source archive.

FFmpeg and its libraries are separate programs invoked through the command
line. FFmpeg is not owned by the AI Landscape project.
