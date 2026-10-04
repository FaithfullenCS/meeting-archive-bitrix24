# Parakeet: самостоятельный локальный движок

Актуальность: 2 октября 2026 года. Приложение поддерживает модель **Parakeet TDT 0.6B v3 Q8** через официальный **NVIDIA NeMo-Speech.cpp 0.1.0**. Пользователь выбирает её в каталоге; сохранение Whisper и выбор другой модели независимы. Весов и native runtime достаточно для распознавания, установка PyTorch или CUDA Toolkit для этого движка не требуется. Системный драйвер NVIDIA остаётся необходимым. [Официальный runtime](https://github.com/NVIDIA/NeMo-Speech.cpp/releases/tag/v0.1.0), [модель](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).

## Закреплённые файлы

| Файл | Источник и версия | Байты | SHA-256 |
|---|---|---:|---|
| Windows x64 CUDA runtime | [NVIDIA v0.1.0](https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v0.1.0/nemo-speech-0.1.0-windows-x86_64-cuda.zip) | 106044768 | `ba024204e76ca2fa4eefa8787506c3c49e418147f627f60cf9206a582b60089c` |
| Windows x64 CPU runtime | [NVIDIA v0.1.0](https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v0.1.0/nemo-speech-0.1.0-windows-x86_64-cpu.zip) | 4730421 | `5e4ea81046012edcd77fd8848de8eefb5a4ba38cc26f52eb544ab184695a75d6` |
| Parakeet Q8 GGUF | [NVIDIA, revision 541d1f99c6b0c3cd0b11a95167540bb8edefd82b](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/resolve/541d1f99c6b0c3cd0b11a95167540bb8edefd82b/parakeet-tdt-0.6b-v3.q8_0.gguf) | 713975456 | `e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e` |

Метаданные сверены с [официальным индексом v0.1.0](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/v0.1.0/models/index.json) и [перечнем release assets](https://github.com/NVIDIA/NeMo-Speech.cpp/releases/expanded_assets/v0.1.0). Установщик проверяет размер, SHA-256 и HTTPS перед сохранением. Распаковка запрещает выход за свою папку и ссылки. Установка с отменой использует временную папку; `ready` появляется только после публикации полного комплекта. Проверенный архив runtime сохраняется для повторной попытки. Профили CUDA и CPU используют один принадлежащий приложению GGUF.

Модель лицензирована CC BY 4.0, runtime — Apache 2.0. Архив сохраняет исходные лицензии, рядом с моделью записывается `MODEL-SOURCES.txt` с атрибуцией NVIDIA. [Лицензия модели](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/blob/main/LICENSE), [лицензия runtime](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/v0.1.0/LICENSE).

## Обработка и ограничения

После общей подготовки звука собственный worker делит PCM16 WAV на 30-секундные фрагменты с контекстом по 0,5 секунды. Максимальный вход одного native запроса — 31 секунда. Runtime загружает модель один раз для папки фрагментов и обрабатывает их последовательно (`--concurrency 1`). Словесные таймкоды возвращаются в исходную шкалу времени, дубли в контексте исключаются по принадлежности середины слова одному фрагменту. Это ограничивает объём одного запроса, но не обещает качество на границах длинного совещания. [CLI v0.1.0](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/v0.1.0/docs/cli.md).

CUDA выбирается явно как `cuda:0`. Перед обработкой запускается native `doctor --json`; при недоступности GPU обработка завершается проверяемой ошибкой. Скрытого переключения на CPU нет. CPU используется только после явного выбора в приложении. Отмена завершает собственный native процесс и его дочерние процессы.

Parakeet v3 распознаёт русский среди 25 поддерживаемых языков автоматически. У этой модели в закреплённом native runtime нет отдельной надёжной оценки вероятности языка. Если поле `languages` отсутствует, результат содержит `language: "und"` (язык не предоставлен движком) и `language_probability: null`. Принудительный выбор языка и начальная текстовая подсказка в этом адаптере не реализованы; подсказка вызывает явную ошибку. [Карточка языков](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), [параметры native ASR](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/v0.1.0/docs/asr/customization.md).

Переключатель пауз группирует результат по промежуткам словесных таймкодов (`pause_detection: "alignment-gaps"`). Предварительное удаление тишины с Silero VAD здесь отсутствует (`vad_preprocessing: false`): для него требуется отдельно подготовленная совместимая модель. Диаризация — дополнительный модуль приложения; метки говорящих Parakeet автоматически не создаёт. Качество исходной FP32 модели из публикаций нельзя автоматически приписать Q8 и этому runtime.
