"""Подготовка видео — чистый модуль поверх ffmpeg (порт из makeVideo).

Не знает ничего о Telegram, каналах и БД: файл + параметры на входе,
обработанный файл на выходе. Границу с приложением держит
:class:`pxcontrol.engine.services.video.VideoService`.
"""

from pxcontrol.engine.video.pipeline import ProcessingOptions, process
from pxcontrol.engine.video.probe import VideoInfo, probe_video

__all__ = ["ProcessingOptions", "VideoInfo", "probe_video", "process"]
