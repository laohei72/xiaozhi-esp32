# 独立 PCM 播放通道设计（百度 TTS 24 kHz PCM 直通）

本文件只描述**架构设计**，不包含任何源码改动。

> **设计前提（已确认）**：百度短文本在线语音合成 API 支持 `aue=4`（PCM 格式）、
> 默认采样率 **24000 Hz**、单声道；ESP32 当前音频输出采样率同为 24000 Hz。
> 因此 PCM 通道**不做任何重采样**：原始字节直接转 `int16_t` 后进入现有播放队列。
> 原有 Opus 解码链路及其 `output_resampler_` 完全不受影响。

| 项 | 值 |
| --- | --- |
| 仓库 | `laohei72/xiaozhi-esp32` |
| 源码基准 commit | `8f5fd641803cc37aebdb4e85c8f0c3fc925d526f` |
| 目标硬件 | ESP32-S3，8 MB PSRAM |
| 板卡音频 | 输入 16000 Hz / 输出 24000 Hz / 单声道 |
| 音源 | 百度在线 TTS，PCM s16le **24000 Hz** 单声道（`aue=4`） |
| 本阶段产物 | 仅本文档；`main/audio/**` 与硬件驱动不做任何修改 |

## 1. 设计目标与硬约束

目标链路：

```text
HTTP chunk (raw PCM s16le 24 kHz mono)
  -> WritePcmData()            // 字节级切片，任意长度；携带会话双令牌
  -> pcm_pending_bytes_        // <= 2880 B 暂存
  -> memcpy 成 int16_t         // 完整帧 1440 samples (= 60 ms)；尾帧可少于 1440
  -> audio_playback_queue_     // FixedQueue<AudioTask, 2>，与 Opus 共用
  -> AudioOutputTask()         // 唯一消费者
  -> codec_->OutputData()
  -> speaker
```

取消令牌有**两套，彼此独立、互不比较**（详见 §6.2 / §6.4）：

- `pcm_stream_generation_`：只由 `AbortPcmPlayback()` 递增。
- `playback_generation_`：只由既有 `Stop()` / `ResetDecoder()` 递增。

硬约束（逐条对应后文实现章节）：

1. 不经过 Opus 解码器（`opus_decoder_` / `esp_opus_dec_decode` 不参与）。
2. 不经过 `OggDemuxer`（即不复用 `PlaySound()`，见 `audio_service.cc:768-790`）。
3. 不改变原有 Opus 播放链路（`OpusCodecTask()`、`PushPacketToDecodeQueue()`、`SetDecodeSampleRate()` 全部原样保留）。
4. 不改变麦克风采集、`ReadAudioData()`、AFE 引擎、WakeNet（这些代码不在新路径的调用链上）。
5. 不修改 `no_audio_codec.cc`。
6. 不修改 I2S 硬件驱动（`AudioCodec::Write()` 及其实现保持原样）。
7. 不引入任何 PCM 重采样器：百度 TTS 直接输出 24 kHz，`BeginPcmPlayback()` 对采样率做严格相等校验，不匹配即拒绝启动；Opus 链路的 `output_resampler_` 保持原样不动。
8. PCM 与 Opus 共用 `AudioOutputTask()`，杜绝并发写扬声器。
9. 支持 HTTP 分块接收，正确处理奇数尾字节，不丢样本、不错位。
10. 所有缓冲区有明确上界。
11. 播放结束、队列排空、错误退出均有确定行为。
12. 取消语义为**软取消**：Abort 之后尚未通过最终令牌检查的帧不得入队，已通过最终检查的帧允许完成入队并自然播完（§6.4.1–§6.4.2）；不承诺立即静音，也不承诺"Abort 与写入返回之间无并发窗口"。
13. PCM 会话存续期间，`Application` 不得发起新的 Opus TTS 播放（§9.5 独占播放契约）。

## 2. 现有播放链路事实核对

设计必须贴合当前源码，以下是本设计依赖的全部事实（行号基于基准 commit）。

| 符号 | 位置 | 语义（本设计依赖点） |
| --- | --- | --- |
| `MAX_PLAYBACK_TASKS_IN_QUEUE` | `audio_service.h:42` = 2 | 播放队列容量，Opus 与 PCM 共用 |
| `OPUS_FRAME_DURATION_MS` | `audio_service.h:40` = 60 | 帧粒度基准，PCM 切片取 60 ms 对齐 |
| `struct AudioTask` | `audio_service.h:98-104` | 唯一播放单元：`type`/`pcm`/`timestamp`/`playback_id`/`media_position_ms` |
| `AudioTaskType` | `audio_service.h:92-96` | 三个枚举值，`AudioOutputTask` **不读取** `type` |
| `output_resampler_` | `audio_service.h:162` | 仅 Opus 解码路径使用；PCM 路径不需要重采样器 |
| `input_resampler_` / `input_resampler_mutex_` | `audio_service.h:160-161` | 采集路径专用，PCM 不得复用 |
| `audio_queue_mutex_` / `audio_queue_cv_` | `audio_service.h:182-183` | 播放队列的唯一同步原语 |
| `audio_playback_queue_` | `audio_service.h:193` | `FixedQueue<AudioTask, MAX_PLAYBACK_TASKS_IN_QUEUE>` |
| `decode_in_flight_` / `output_in_flight_` | `audio_service.h:194-195` | in-flight 标记，参与 drain 判定 |
| `playback_drained_notified_` | `audio_service.h:196` | drain 回调去重标志 |
| `playback_generation_` | `audio_service.h:197` | 普通 `uint32_t`（受 `audio_queue_mutex_` 保护），`Stop()` / `ResetDecoder()` 递增，用于全局代际取消 |
| `IsPlaybackDrainedLocked()` / `MarkPlaybackDrainedLocked()` | `audio_service.cc:824-835` | drain 判定与回调触发 |
| `AudioOutputTask()` | `audio_service.cc:326-379` | 唯一扬声器消费者 |
| `OpusCodecTask()` 解码段 | `audio_service.cc:393-464` | 生产 `AudioTask` 的既有范式（含背压与代际检查） |
| `PushPacketToDecodeQueue()` | `audio_service.cc:609-629` | 阻塞背压 + `generation` 取消的既有范式 |
| `SetDecodeSampleRate()` | `audio_service.cc:532-567` | 创建/关闭 `output_resampler_` 的既有写法 |
| `PlaySound()` | `audio_service.cc:768-790` | Ogg→Opus 路径，**不能**用于裸 PCM |
| `ResetDecoder()` | `audio_service.cc:802-822` | 会 `++playback_generation_` 并清空播放队列，PCM 不得用它收尾 |
| `RATE_CVT_CFG` 宏 | `audio_service.cc:5-10` | 仅 Opus 路径使用；PCM 路径不涉及重采样 |
| `AudioCodec::OutputData()` | `audio_codec.h:37` / `audio_codec.cc:17-19` | 直接 `Write(data.data(), data.size())` |
| `AudioCodec::output_sample_rate()` | `audio_codec.h:44` | 目标采样率 24000 的唯一来源 |
| `FixedQueue::push_back()` | `main/audio/fixed_queue.h:39-46` | 满时返回 `false`，不阻塞、不覆盖 |
| `esp_ae_rate_cvt_*` | `esp_ae_rate_cvt.h:87/101/122/165/172` | 仅被 `SetDecodeSampleRate()` 用于 Opus；PCM 路径**不调用**这些接口 |

关键结论：

- `AudioOutputTask()` 只使用 `task.pcm`、`task.playback_id`、`task.media_position_ms`、`task.timestamp`，
  从不分支 `task.type`（`audio_service.cc:335-366`）。因此 PCM 帧可以直接复用
  `kAudioTaskTypeDecodeToPlaybackQueue`，**无需修改共享枚举**。
- 播放队列是**单消费者**模型：只要 PCM 不直接调用 `codec_->OutputData()`，
  就不可能并发写 I2S。这一点由架构保证，不需要额外互斥。
- `AudioCodec::OutputData()` 直接把 `data.size()` 个 `int16_t` 交给 `Write()`
  （`audio_codec.cc:17-19`），不做任何采样率换算。因此 `AudioTask.pcm` 的样本数**必须已经是
  输出采样率的样本数**：百度 TTS 直出 24 kHz，与 `codec_->output_sample_rate()` 相同，
  天然满足，不需要任何转换代码。

## 3. 新增接口

全部新增在 `AudioService`（`audio_service.h`）。公有入口 5 个，私有辅助 2 个；不修改任何现有函数签名。

### 3.1 公有接口

```cpp
// 开始一次 PCM 播放会话。要求 input_sample_rate == codec_->output_sample_rate()
// （当前板卡 24000 Hz），且 input_channels == 1；不满足即返回 false，绝不做隐式重采样。
//
// 进入 active 时，把两套取消代际各快照一次，作为本次会话**固定不变**的取消令牌：
//     pcm_session_pcm_gen_      = pcm_stream_generation_.load()   // 只由 AbortPcmPlayback() 递增
//     pcm_session_playback_gen_ = playback_generation_            // 只由 Stop()/ResetDecoder() 递增
// 其中 playback_generation_ 是受 audio_queue_mutex_ 保护的普通变量，快照必须在
// audio_queue_mutex_ 内读取（锁序 pcm_mutex_ -> audio_queue_mutex_，见 §6.2.1）。
// 会话期间这两个字段只读，绝不重新读取当前代际来替换（§6.2）。
//
// playback_id 透传给 AudioTask::playback_id，
// 非 0 时会触发既有的 callbacks_.on_playback_progress。
// 返回 false 表示参数非法（此时不进入 active 状态）。
bool BeginPcmPlayback(int input_sample_rate, int input_channels, uint32_t playback_id = 0);

// 写入任意长度 HTTP chunk（小端 s16le 原始字节）。
// last == true 表示这是本次播放的最后一块。
// 返回 false 表示会话已被 EndPcmPlayback()/AbortPcmPlayback()/Stop()/ResetDecoder() 取消；
// 调用方应停止推送并关闭 HTTP 连接。
bool WritePcmData(const uint8_t* data, size_t size, bool last = false);

// 结束会话：把残余样本作为短帧入队，然后等待播放队列排空。
// 三条返回路径（详见 §9.1）：
//   1) 正常排空            -> true
//   2) 等待期间被取消       -> false，立即返回，不等满 timeout
//      （AbortPcmPlayback() / Stop() / ResetDecoder()，或尾帧入队时令牌已失效）
//   3) 仅等待超时           -> false，但不会强杀队列（避免误伤并发的 Opus 音频）
// 排空判定是全局的（IsPlaybackDrainedLocked），因此要求遵守 §9.5 独占播放契约。
// 无论走哪条路径，返回时 pcm_stream_active_ 均已为 false（会话不可再写，§9.4）。
bool EndPcmPlayback(uint32_t timeout_ms = 5000);

// 立即取消：只递增 pcm_stream_generation_（不触碰 playback_generation_），
// 丢弃暂存字节并唤醒可能阻塞的写入者。
// 已通过检查并入队/正在播放的 PCM 帧会自然播完：残留上限 3 帧 ≈ 180 ms（软取消，§6.4）。
// 本函数**不保证立即静音**；需要整体静音由上层调用 Stop()/ResetDecoder()（会同时影响 Opus）。
void AbortPcmPlayback();

// 返回 pcm_stream_active_（会话是否处于"可写"状态）。
// 它**只反映可写标志**，不保证两套令牌仍然有效：
//   - AbortPcmPlayback() 会把该标志清零，之后返回 false；
//   - Stop() / ResetDecoder() 不会清零该标志，因此在 EndPcmPlayback() 之前的**短暂期间**
//     仍可能返回 true，但此时 WritePcmData() 一定返回 false（播放令牌失配）。
// 判断"写入是否还会被接受"的唯一权威依据是 WritePcmData() / EndPcmPlayback() 的返回值（§9.4）。
bool IsPcmPlaybackActive() const;
```

命名风格与现有 `PushPacketToDecodeQueue()` / `PlaySound()` / `EnableAudioTesting()` 保持一致（动词开头、对象明确、无缩写歧义）。

### 3.2 私有辅助

```cpp
// 生产者入口：把一帧（24 kHz 直通样本）送入共用播放队列。
// 两个形参是本次会话的**固定取消令牌快照**，来自 BeginPcmPlayback()，函数内部
// 不得重新 load 当前代际，也不得把两个令牌相互比较（§6.2）。
// 队列满时在 audio_queue_cv_ 上阻塞（背压）；两套令牌的检查与 push_back 在
// 同一个 audio_queue_mutex_ 临界区内完成。调用方必须持有 pcm_mutex_。
// 返回 false 表示被取消或服务已停止。
bool PushPcmTaskToPlaybackQueue(std::vector<int16_t>&& pcm,
                                uint32_t pcm_generation,
                                uint32_t playback_generation);

// 把 pcm_pending_bytes_ 按完整帧（1440 samples @24 kHz）切分、转 int16_t 并推送。
// is_final == true 时允许推送长度不足 1440 samples 的尾帧。
// 两个令牌原样透传给 PushPcmTaskToPlaybackQueue()，本函数不自行读取代际。
// 调用方必须持有 pcm_mutex_。
bool FlushPcmPendingBytes(bool is_final,
                          uint32_t pcm_generation,
                          uint32_t playback_generation);
```

> 两个令牌参数的命名刻意区分来源：`pcm_generation` 对应 `pcm_stream_generation_`，
> `playback_generation` 对应 `playback_generation_`。二者**没有数值关系**，
> 全文禁止出现 `pcm_generation != playback_generation` 之类的交叉比较。

### 3.3 行为契约

| 场景 | 行为 |
| --- | --- |
| 未调用 `BeginPcmPlayback()` 就写数据 | `WritePcmData()` 返回 `false`，日志 `ESP_LOGW`，不改任何状态 |
| 重复调用 `BeginPcmPlayback()` | 先隐式 `AbortPcmPlayback()` 再新建会话（不嵌套），新会话重新快照两套令牌 |
| `WritePcmData()` 中 `size == 0` | **先做 `pcm_stream_active_` 检查**：会话有效时视为 no-op 返回 `true`（HTTP 空块合法）；未 `BeginPcmPlayback()` 时返回 `false`（与"未 Begin 不允许写入"一致，§7.2） |
| `size == 0 && last == true` | 视为"推送尾帧"：走一次 `FlushPcmPendingBytes(true, ...)`，把暂存的残余完整样本推完 |
| `WritePcmData()` 短块（本次调用未凑满 2880 B，未触发帧化）且期间已被 Abort | 返回 `false`：返回前用**会话固定令牌**做一次权威检查（取一次 `audio_queue_mutex_`，不阻塞，§7.2） |
| `WritePcmData()` 与 `AbortPcmPlayback()` 并发 | 允许（取消阻塞写入所必需）。`WritePcmData()` 返回 `true` 只表示"该批字节已被接受"，**不表示** Abort 尚未发生；不存在"Abort 与写入返回之间无并发窗口"的保证（§7.2） |
| `last == true` 且 `size` 为奇数 | 最后 1 字节无法构成样本，`ESP_LOGW` 丢弃；其余完整样本全部播放 |
| `BeginPcmPlayback()` 采样率 ≠ `codec_->output_sample_rate()` | 返回 `false` + `ESP_LOGE`，不进入 active（本设计不做重采样） |
| `BeginPcmPlayback()` 时采样率与输出一致 | 正常进入 active，字节流原样转 `int16_t` 直通播放，并快照两套令牌 |
| 会话期间 `AbortPcmPlayback()` 之后继续写 | `WritePcmData()` 返回 `false`（**PCM 令牌**失配）；已入队帧自然播完，残留 ≤ 3 帧 ≈ 180 ms（§6.4） |
| 会话期间 `Stop()` / `ResetDecoder()` 之后继续写 | `WritePcmData()` 返回 `false`（**播放令牌**失配）；既有实现已清空播放队列，残留仅 in-flight 1 帧 ≈ 60 ms（§6.4） |
| `service_stopped_` 为真 | 所有写入立即返回 `false` |
| `EndPcmPlayback()` 因取消（Abort/Stop/ResetDecoder）或尾帧入队失败退出 | 返回 `false`，`pcm_stream_active_` **已置 `false`**；调用方**不要求**再调用 `AbortPcmPlayback()`（它仍是幂等的，可调用），但必须关闭 HTTP 连接并丢弃后续数据（§9.1、§9.4） |
| `BeginPcmPlayback()` 与 `AbortPcmPlayback()` 并发 | **契约禁止**（控制面必须串行，§6.3）；不新增锁来兜底 |
| 队列层面出现 Opus 帧（违反 §9.5 契约时） | 结构上按帧 FIFO 交错播放，`AudioOutputTask` 仍然是唯一消费者；但该状态属于上层违约，`EndPcmPlayback()` 的返回时间将不可预测 |

## 4. 新增队列与数据结构

### 4.1 新增常量（`audio_service.h`，紧随现有 `#define` 区块）

```cpp
#define PCM_PLAYBACK_FRAME_DURATION_MS 60    // 与 OPUS_FRAME_DURATION_MS 对齐
#define PCM_PLAYBACK_CHUNK_SAMPLES     1440  // 60 ms @ 24 kHz 单声道
#define PCM_PLAYBACK_CHUNK_BYTES       (PCM_PLAYBACK_CHUNK_SAMPLES * sizeof(int16_t))  // 2880
#define PCM_PLAYBACK_DRAIN_TIMEOUT_MS  5000
```

取 60 ms 而非 10/20 ms 的理由：与 `OPUS_FRAME_DURATION_MS` 完全一致，`AudioOutputTask` 的
单帧写入耗时、`on_playback_progress` 粒度、播放队列容量（2 帧）语义都不需要重新标定；
20 ms 会让队列只有 40 ms 缓冲，容易在 `OutputData()` 阻塞时抖动。

### 4.2 新增成员（全部为新增，不改变现有成员语义）

| 成员 | 类型 | 容量/上界 | 说明 |
| --- | --- | --- | --- |
| `pcm_pending_bytes_` | `std::vector<uint8_t>` | **≤ 2880 B**（`reserve` 一次，不自动增长超过上界） | HTTP 字节级暂存，跨块残料 |
| `pcm_mutex_` | `std::mutex` | - | 保护以上全部 PCM 状态（对应 `input_resampler_mutex_` 的既有做法） |
| `pcm_stream_generation_` | `std::atomic<uint32_t>` | - | **活动** PCM 代际；只由 `AbortPcmPlayback()` 在**取锁前**自增 |
| `pcm_session_pcm_gen_` | `uint32_t` | - | 会话令牌：`BeginPcmPlayback()` 快照的 `pcm_stream_generation_`，会话期间只读 |
| `pcm_session_playback_gen_` | `uint32_t` | - | 会话令牌：`BeginPcmPlayback()` 快照的 `playback_generation_`，会话期间只读 |
| `pcm_stream_active_` | `bool` | - | 会话是否可写；只由 `BeginPcmPlayback()`（置真）与 `EndPcmPlayback()` / `AbortPcmPlayback()`（置假）修改，`Stop()` / `ResetDecoder()` 不改它（§9.4） |
| `pcm_playback_id_` | `uint32_t` | - | 透传给 `AudioTask::playback_id` |
| `pcm_media_position_ms_` | `uint32_t` | - | 透传给 `AudioTask::media_position_ms`（取**入队前**的媒体时间，见 §6.2） |

> 奇数尾字节**不需要**独立的 carry 字段：`pcm_pending_bytes_` 以「字节」为暂存单位，
> 尾字节天然留在缓冲里等待下一个 chunk 拼接（详见 §7）。这是本设计唯一需要关心的
> 对齐问题，用字节暂存即可彻底解决，避免再引入 `uint8_t`/`bool` 这类跨会话状态。

> 说明：**不新增播放队列**。需求 8 要求与 Opus 共用 `AudioOutputTask()`，
> 因此复用 `audio_playback_queue_`（`audio_service.h:193`）就是唯一正确选择；
> 新增第二个队列会立刻引入两个消费者竞争 I2S 的问题。

> 说明（移除重采样后的精简结果）：不再需要重采样器句柄、scratch 缓冲、源采样率字段，
> 成员共 8 个（含 2 个会话令牌快照），私有辅助函数 2 个。`pcm_mutex_` 仍然必需 ——
> 它保护 `pcm_pending_bytes_` 的「写入 + 帧化」复合操作以及媒体时间/令牌快照，
> 并保证 `EndPcmPlayback()` 不会与正在进行的 `memcpy`/`erase` 竞争；
> 而 `pcm_stream_generation_` **不能**用这把锁替代，因为它必须在**取锁前**自增
> 才能破除写入者在 `audio_queue_cv_` 上的阻塞（§6.4）。
> 注意：`pcm_mutex_` **不**参与"检查令牌 + 入队"的原子性，那由 `audio_queue_mutex_` 保证（§6.2）。

### 4.3 帧化结构

完整帧 = 1440 samples（2880 B）@24 kHz = 60 ms；**尾帧允许少于 1440 samples**
（`FlushPcmPendingBytes(true)` 时按实际字节数推送）。除尾帧外，
`AudioTask.pcm.size() == PCM_PLAYBACK_CHUNK_SAMPLES` 成立。

音源与输出采样率相同，帧内容就是原始字节，无需任何转换。
单帧缓冲 2880 B；把各路缓冲区**各自独立**的上界相加得 14400 B ≈ 14.4 KB（见 §8.1）。
该数字是**保守的上界之和**，**不是**"同一瞬间必然同时出现 5 块满载数据"，
也不是全链路内存占用；实际稳态峰值明显更低（§8.1 说明 2–4）。

## 5. 采样率约束与字节 → `int16_t` 直通转换

重采样已整体移除（百度 TTS 直出 24 kHz）。本章说明取代它的三件事：采样率校验、
字节到 `int16_t` 的转换规则、以及"不做重采样"之后的边界条件。

### 5.1 采样率校验（`BeginPcmPlayback()` 内，持 `pcm_mutex_`）

```cpp
const int out_rate = codec_->output_sample_rate();   // 当前板卡 24000
if (input_sample_rate != out_rate || input_channels != 1) {
    ESP_LOGE(TAG, "Unsupported PCM format: %d Hz / %d ch (output is %d Hz mono)",
             input_sample_rate, input_channels, out_rate);
    return false;                                    // 不进入 active，不做隐式重采样
}
```

- **严格相等**而非"可转换即通过"：一旦允许不等，就必须重新引入 `esp_ae_rate_cvt`，
  违背本次简化前提。上层必须把百度 TTS 请求固定为 `aue=4` + `sampling_rate=24000`。
- 与 `codec_->output_sample_rate()` 这个**运行时值**比较，而非编译期常量，
  避免将来换板卡（输出 16 kHz）时静默产生变调音频。
- 校验失败不改变任何状态，`pcm_stream_active_` 保持 `false`，可安全重复调用。

### 5.2 字节 → `int16_t` 的转换规则

```cpp
std::vector<int16_t> frame(byte_count / sizeof(int16_t));
std::memcpy(frame.data(), pcm_pending_bytes_.data(), byte_count);
pcm_pending_bytes_.erase(pcm_pending_bytes_.begin(),
                         pcm_pending_bytes_.begin() + byte_count);
```

- **单位**：`AudioTask.pcm` 是 `std::vector<int16_t>`（`audio_service.h:98-104`），
  长度单位是**样本数**而非字节数，所以 `byte_count` 必须先保证为偶数，再除以
  `sizeof(int16_t)` 得到样本数（§7 的切片逻辑保证这一点）。
  完整帧 `byte_count = 2880`（1440 samples）；`is_final == true` 时尾帧的
  `byte_count` 可以是 2 ~ 2878 之间的偶数值，对应 `1 ~ 1439` samples。
- **字节序**：`memcpy` 直接把内存当 `int16_t` 数组使用，隐含要求主机小端；
  ESP32-S3（XTENSA）为小端，与百度 PCM `s16le` 一致。不做逐样本拼装，省掉移位运算。
- **对齐**：源缓冲是 `uint8_t` 向量，`memcpy` 不要求源对齐；且每次帧化都从
  `pcm_pending_bytes_` 的**头部**取数据、取完立即 `erase`，因此 `memcpy` 的源偏移恒为 0
  （不会出现"从奇数偏移开始拷"的情况），目标 `frame` 由 `std::vector<int16_t>` 保证对齐。
- **无插值、无滤波**：24 kHz 进、24 kHz 出，音调、语速、时长与服务器合成结果完全一致；
  "语速变化"只可能来自服务器参数，不来自设备端处理。
- 该片段位于 `FlushPcmPendingBytes()` 内（`pcm_mutex_` 保护下）；令牌透传与
  媒体时间推进的完整版本见 §7.3，不要把本片段单独实现成"无令牌、无媒体时间"的变体。

### 5.3 移除重采样后的边界条件

| 边界 | 结论 | 依据 |
| --- | --- | --- |
| 帧长（完整帧） | 1440 samples = 2880 B = 60 ms；尾帧可更小，按实际样本数计时 | 24000 × 0.06 |
| 采样率漂移 | 不存在：链路上没有转换环节 | I2S 时钟仍由 `AudioCodec` 既有配置决定 |
| 首帧延迟 | 无重采样器预热/预填充延迟 | 首帧入队即可播 |
| 尾帧 | 可直接推送 `< 1440` samples 的短帧 | `AudioCodec::OutputData()` 接受任意长度 |
| 内存 | 无重采样器内部状态，也无 scratch 缓冲 | 见 §8.1 |

- 若百度 TTS 被改成其它采样率（例如 16000），本设计的正确行为是**拒绝播放并报错**，
  而不是悄悄变调；这比隐式重采样的失败模式更容易定位。

## 6. 与现有 `AudioOutputTask` 的协同

### 6.1 复用原则

- **唯一写扬声器者**仍是 `AudioOutputTask()`（`audio_service.cc:326-379`），最终调用
  `codec_->OutputData(task.pcm)`（`audio_service.cc:351` → `audio_codec.cc:17-19`）。
- PCM 生产方只做两件事：把 `AudioTask` 塞进 `audio_playback_queue_`，以及在队列满时阻塞。
  它**绝不**调用 `codec_->OutputData()`、`codec_->Write()`、`ESP_OK == i2s_channel_write()`。
- 复用 `AudioTask`（`audio_service.h:98-104`），字段填充方式与
  `OpusCodecTask()`（`audio_service.cc:403-407`）一致：

  | 字段 | PCM 路径取值 |
  | --- | --- |
  | `type` | `kAudioTaskTypeDecodeToPlaybackQueue`（`AudioOutputTask` 不读该字段，见 §2） |
  | `pcm` | 24 kHz 直通帧：完整帧 1440 samples = 2880 B = 60 ms；**尾帧可少于 1440 samples** |
  | `timestamp` | `0`（不参与 server AEC；见 §10.4） |
  | `playback_id` | `pcm_playback_id_` |
  | `media_position_ms` | `pcm_media_position_ms_`，取**本帧入队前**的媒体时间；入队成功后才推进（§6.2、§7.3） |

- 不需要修改 `Start()` 里创建 `audio_output` 任务的代码（`audio_service.cc:136-160`），
  也不需要修改 `audio_output_task_handle_`。

### 6.2 入队实现（阻塞背压 + 双令牌取消 + 原子入队）

对齐 `PushPacketToDecodeQueue()`（`audio_service.cc:609-629`）的既有范式，但**必须使用两套独立令牌**。

#### 6.2.1 会话开始时的令牌快照（`BeginPcmPlayback()` 内）

```cpp
// 锁序沿用 pcm_mutex_ -> audio_queue_mutex_。
// playback_generation_ 是受 audio_queue_mutex_ 保护的普通 uint32_t（audio_service.h:197），
// 因此快照必须在 audio_queue_mutex_ 内读取，否则是数据竞争。
std::lock_guard<std::mutex> pcm_lock(pcm_mutex_);
std::lock_guard<std::mutex> queue_lock(audio_queue_mutex_);

pcm_session_pcm_gen_      = pcm_stream_generation_.load();  // std::atomic，随 AbortPcmPlayback() 变化
pcm_session_playback_gen_ = playback_generation_;           // 随 Stop()/ResetDecoder() 变化
pcm_stream_active_        = true;
pcm_media_position_ms_    = 0;
pcm_pending_bytes_.clear();
```

- 两个令牌**只在这里写一次**，会话期间只读；`WritePcmData()` / `FlushPcmPendingBytes()` /
  `PushPcmTaskToPlaybackQueue()` 一律使用这两份快照，**不得**重新 `load()` 当前代际去替换。
- 重复调用 `BeginPcmPlayback()` 时先隐式 `AbortPcmPlayback()`（§3.3），再取上述快照，
  因此新会话拿到的是 Abort 之后的 PCM 代际，旧会话的所有令牌立即失效。
- 两套令牌之间**没有任何数值关系**（各自独立递增），全文禁止交叉比较
  （例如 `pcm_generation != playback_generation` 属于逻辑错误）。
- 快照在 `audio_queue_mutex_` 内完成，因此与 `Stop()` / `ResetDecoder()` 的 `++playback_generation_`
  严格有序：要么拿到递增前的值（随后被判定失效），要么拿到递增后的值（新会话有效）。
- **调用方串行化要求（不新增锁，只作为接口契约）**：`BeginPcmPlayback()` / `EndPcmPlayback()` /
  `AbortPcmPlayback()` 三者属于**控制面**操作，必须由同一个控制线程（`Application` 侧）串行调用。
  其中**特别禁止 `BeginPcmPlayback()` 与 `AbortPcmPlayback()` 并发启动/初始化**：
  `BeginPcmPlayback()` 在完成快照之后才置 `pcm_stream_active_ = true`，若 Abort 恰好在其间完成，
  会得到"`active` 为真、但两套令牌均已失效"的会话 —— 这不是数据竞争（所有访问都在锁内），
  而是**接口层面的未定义状态**。本节不引入新锁来掩盖它，而是把它明确写成调用方契约（§6.3）。
- `WritePcmData()` 与 `AbortPcmPlayback()` 的并发是**唯一允许**的并发组合，
  目的是让 Abort 能打断阻塞在队列背压上的写入；完整组合矩阵见 §6.3。

#### 6.2.2 生产者：检查与入队在同一临界区（`PushPcmTaskToPlaybackQueue()`）

```cpp
bool AudioService::PushPcmTaskToPlaybackQueue(std::vector<int16_t>&& pcm,
                                              uint32_t pcm_generation,
                                              uint32_t playback_generation) {
    // 调用方（FlushPcmPendingBytes）必须已持有 pcm_mutex_
    AudioTask task;
    task.type = kAudioTaskTypeDecodeToPlaybackQueue;
    task.pcm = std::move(pcm);
    task.playback_id = pcm_playback_id_;
    task.media_position_ms = pcm_media_position_ms_;   // 入队前的媒体时间，本函数不推进

    std::unique_lock<std::mutex> lock(audio_queue_mutex_);

    // 谓词只负责唤醒：队列有空间，或任一令牌已失效，或服务已停止
    audio_queue_cv_.wait(lock, [&] {
        return service_stopped_.load() ||
               pcm_generation      != pcm_stream_generation_.load() ||  // 只与 PCM 代际比
               playback_generation != playback_generation_ ||           // 只与播放代际比
               audio_playback_queue_.size() < MAX_PLAYBACK_TASKS_IN_QUEUE;
    });

    // ==== 最终判定：与 push_back 处于同一个 audio_queue_mutex_ 临界区，且紧邻其前 ====
    if (service_stopped_.load()) return false;
    if (pcm_generation      != pcm_stream_generation_.load()) return false;
    if (playback_generation != playback_generation_) return false;

    playback_drained_notified_ = false;   // 与 audio_service.cc:625 同理，保证 drain 回调会再次触发
    if (!audio_playback_queue_.push_back(std::move(task))) {
        ESP_LOGE(TAG, "Failed to push PCM task: playback queue is full");
        return false;
    }
    audio_queue_cv_.notify_all();
    return true;                          // 唯一成功出口
}
```

要点：

1. **原子性边界必须按代际分别陈述**（不得笼统宣称"两套令牌在检查与入队之间都没有变化窗口"）：
   - `playback_generation_`（由 `Stop()` / `ResetDecoder()` 递增）：该变量受 `audio_queue_mutex_` 保护，
     两个函数都在持有同一把锁时执行 `++`，因此**最终检查通过后到 `push_back` 之间不存在代际变化窗口** ——
     同一临界区内其他线程无法执行 `++playback_generation_`。
   - `pcm_stream_generation_`（由 `AbortPcmPlayback()` 递增）：Abort **不获取** `audio_queue_mutex_`，
     所以最终检查通过之后、`push_back` 真正执行之前，该代际**仍可能被递增**。
     本设计接受这一窗口：该帧已通过最终检查，允许完成入队（软取消，§6.4.2）。
     对 PCM 令牌只保证单向性质：**尚未通过最终检查的帧不会在 Abort 之后入队**。
   - 因此这里的保证是"检查与入队位于同一临界区"的**一致性**，
     **不是**"两套令牌都没有窗口"的强断言。不能只依赖 wait 谓词，也不能在取锁前检查后直接入队。
2. **不得用 `pcm_mutex_` 充当这层原子性**：`AbortPcmPlayback()` 自增 PCM 代际时刻意不取
   `audio_queue_mutex_`，`pcm_mutex_` 也保护不到 `playback_generation_`。
3. 形参 `pcm_generation` 只与 `pcm_stream_generation_` 比较，形参 `playback_generation`
   只与成员 `playback_generation_` 比较 —— **两者永不相互比较**（§1、§3.2）。
   实现时形参与成员仅差尾部下划线，建议在函数首行各取一个局部别名，
   避免 `-Wshadow` 告警与误读。
4. 谓词里的容量条件与 `OpusCodecTask()` 的等待条件（`audio_service.cc:384-395`）完全一致，
   两个生产者遵守同一容量契约（`MAX_PLAYBACK_TASKS_IN_QUEUE = 2`）。
5. `playback_drained_notified_ = false` 不可省略：它在 `MarkPlaybackDrainedLocked()`
   （`audio_service.cc:829-835`）里是去重标志，不重置就不会再触发
   `callbacks_.on_playback_drained()`。
6. `push_back()` 返回 `bool`（`fixed_queue.h:39-46`），理论上有谓词保证必成功；
   仍保留返回值检查并记 `ESP_LOGE`，防止谓词与插入之间出现未预期的第三个生产者。
7. **媒体时间不在本函数内推进**：`task.media_position_ms` 必须取"本帧入队前"的值，
   推进由 `FlushPcmPendingBytes()` 在成功返回后按实际帧长执行（§6.2.3、§7.3）。
8. 本函数的成功只表示"帧已进入队列"，**不表示**该帧取消后会被撤回 —— 这是软取消的固有语义（§6.4）。

#### 6.2.3 媒体时间推进（`FlushPcmPendingBytes()` 内，成功入队之后）

```cpp
const uint32_t frame_samples = (uint32_t)frame.size();          // 完整帧 1440；尾帧可能更小
if (!PushPcmTaskToPlaybackQueue(std::move(frame), pcm_generation, playback_generation)) {
    return false;                                               // 失败不推进媒体时间
}
// 仅成功入队后才推进；整数毫秒向下取整
pcm_media_position_ms_ += frame_samples * 1000u / 24000u;        // 1440 -> 60；尾帧 -> 实际时长
```

- 完整帧：`1440 * 1000 / 24000 = 60` ms。
- 尾帧：`duration_ms = sample_count * 1000 / 24000`，整数毫秒**向下取整**，
  因此尾帧时间戳**不严格对齐 60 ms 栅格**，这是允许且明确接受的。
- 禁止在入队前预递增：否则 `task.media_position_ms` 会偏移一帧，
  `on_playback_progress` 与 `AUDIO_POWER_TIMEOUT_MS` 相关日志都会跟着错位。

### 6.3 锁序与死锁分析

固定锁序：`pcm_mutex_` → `audio_queue_mutex_`（绝不允许反向）。

| 任务 | 获取顺序 |
| --- | --- |
| HTTP 写入任务 / `Application` 任务 | `pcm_mutex_` → `audio_queue_mutex_`（`BeginPcmPlayback()` 取令牌快照、`FlushPcmPendingBytes()` 入队时短暂持有；队列满时会在 `pcm_mutex_` 内阻塞等待；绝不反向） |
| `AudioOutputTask` | 仅 `audio_queue_mutex_`（`audio_service.cc:328/358`） |
| `OpusCodecTask` | 仅 `audio_queue_mutex_`（外层 `lock`）+ `decoder_mutex_`（内部 `audio_service.cc:425-427`） |
| `AudioInputTask` | `input_resampler_mutex_`（`audio_service.cc:208`） |

**调用方并发契约**（不新增锁，仅为接口约定；违反属上层违约）：

| 并发组合 | 是否允许 | 说明 |
| --- | --- | --- |
| `WritePcmData()` ‖ `AbortPcmPlayback()` | **允许** | 取消阻塞写入所必需：Abort 先原子自增 + `notify_all()`，再取 `pcm_mutex_` |
| `EndPcmPlayback()` ‖ `AbortPcmPlayback()` | **允许** | End 在排空等待谓词里比较**会话固定 PCM 令牌**，识别后立即返回 `false`（§9.1）；End 等待期间只持 `audio_queue_mutex_`，不会卡住 Abort |
| `BeginPcmPlayback()` ‖ `AbortPcmPlayback()` | **禁止** | 会在"已快照令牌"与"置 `active`"之间插入一次取消，得到 `active` 为真但令牌失效的会话（§6.2.1） |
| `BeginPcmPlayback()` ‖ `EndPcmPlayback()` | **禁止** | 控制面必须串行（同一控制线程） |
| `BeginPcmPlayback()` ‖ `BeginPcmPlayback()` | **禁止** | 同上；重复 Begin 只能**串行**调用（先隐式 Abort，§3.3） |
| `EndPcmPlayback()` ‖ `EndPcmPlayback()` | **禁止** | 同上；排空等待期间不应有第二个 End |
| `WritePcmData()` ‖ `WritePcmData()` | **禁止** | `pcm_pending_bytes_` 与媒体时间只允许单写者，多写者会让字节顺序与时间戳不可预测 |
| `WritePcmData()` ‖ `EndPcmPlayback()` | **禁止** | End 会推送尾帧并关闭 `active`，写入必须在此之前结束 |

- 上述"禁止"均**不通过新增锁实现**，而由 `Application` 侧的串行调度保证（与 §9.5 同一层仲裁）。
- `EndPcmPlayback()` 在排空等待时**只持有 `audio_queue_mutex_`**，不会回头去取 `pcm_mutex_`，
  因此不存在 `audio_queue_mutex_` → `pcm_mutex_` 的反向获取，锁序保持不变；
  它与 `AbortPcmPlayback()`（先原子自增、后取 `pcm_mutex_`）可以安全并发。
- `pcm_mutex_` 与 `input_resampler_mutex_`、`decoder_mutex_` 互不相交，因此
  PCM 不会拖慢麦克风采集或 Opus 解码。
- 关键实现约束：**`memcpy` 帧化必须在进入 `audio_queue_mutex_` 之前完成**。
  `FlushPcmPendingBytes()` 先在 `pcm_mutex_` 内把字节拷进 `std::vector<int16_t>`，
  之后才调用 `PushPcmTaskToPlaybackQueue()` 获取 `audio_queue_mutex_`，
  因此该临界区只包含"谓词等待 + 两套令牌最终检查 + `push_back` + `notify_all`"，
  不会给 `AudioOutputTask` 的取帧间隙增加任何拷贝开销（拷贝量 ≤ 2880 B）。
- 注意：PCM 写入者会**在持有 `pcm_mutex_` 的状态下**阻塞在 `audio_queue_cv_` 上（§7.3）。
  这是有意设计 —— `pcm_mutex_` 只保护 PCM 自身状态，不参与任何音频实时路径。
  代价是 `AbortPcmPlayback()` 必须先用原子 generation + `notify_all()` 破除该等待（见下一条）。
- `AbortPcmPlayback()` 必须在**取 `pcm_mutex_` 之前**自增 `pcm_stream_generation_`：

  ```cpp
  void AudioService::AbortPcmPlayback() {
      pcm_stream_generation_.fetch_add(1);   // ① 原子，不取锁
      audio_queue_cv_.notify_all();          // ② 唤醒可能阻塞在播放队列上的写入者
      std::lock_guard<std::mutex> lock(pcm_mutex_);  // ③ 写入者已被唤醒并返回 false
      pcm_pending_bytes_.clear();
      pcm_stream_active_ = false;
  }
  ```

  顺序颠倒会死锁：写入者阻塞在 `audio_queue_cv_` 上时仍持有 `pcm_mutex_`，
  若 Abort 先抢 `pcm_mutex_` 就永远等不到。
- `AbortPcmPlayback()` 全程**不获取 `audio_queue_mutex_`**，因此它无法与 `push_back` 互斥 ——
  这正是软取消窗口的来源（§6.4.2）。它只递增 `pcm_stream_generation_`，
  **不触碰 `playback_generation_`**，也不清空播放队列。
- `audio_queue_cv_` 是**共享**条件变量，`notify_all()` 会额外唤醒
  `OpusCodecTask` / `AudioOutputTask`，它们醒来后发现谓词不满足会继续等，
  行为与现有 `Stop()`（`audio_service.cc:188`）一致，无副作用。

### 6.4 同步保证与软取消语义（明确采用路线 B）

本设计**明确采用软取消**，不实现"绝对取消"，也不声称取消后立即静音。

#### 6.4.1 同步保证

1. 两套令牌的检查与 `audio_playback_queue_.push_back()` 在**同一个 `audio_queue_mutex_` 临界区**内完成，
   中途不释放锁（§6.2.2 要点 1）。
2. 但该临界区对两套代际提供的保证强度**不同**，必须分别陈述：
   - `playback_generation_` 受同一把锁保护，`Stop()` / `ResetDecoder()` 自增时也持锁 ⇒
     最终检查与入队之间**不存在**代际变化窗口；
   - `pcm_stream_generation_` 由 `AbortPcmPlayback()` 在**不持该锁**的情况下自增 ⇒
     最终检查通过后、`push_back` 执行前**仍可能**被递增 ⇒ 存在软取消窗口。
3. 因此本设计的保证是**单向**的：Abort 之后，尚未通过最终令牌检查的帧不得入队；
   已经通过最终检查的帧允许完成入队，即使实际 `push_back` 发生在 Abort 之后。
4. 该窗口被**明确接受**，不属于遗漏或竞态缺陷。
5. `Stop()` / `ResetDecoder()` 通过 `playback_generation_` 使当前 PCM 会话失效（§6.4.4），
   影响范围同时覆盖 Opus。
6. 任何表述都不得承诺"取消即静音"、"已入队帧会被撤回"，
   也不得承诺"两套令牌在检查后到入队前都没有变化窗口"。

#### 6.4.2 软取消窗口（路线 B）

| 步骤 | 执行者 | 动作 | `audio_queue_mutex_` |
| --- | --- | --- | --- |
| ① | PCM 生产者 | 取得 `audio_queue_mutex_` | 持有 |
| ② | PCM 生产者 | 检查两套令牌，均匹配 | 持有 |
| ③ | PCM 生产者 | `push_back()`（1 帧） | 持有 |
| ④ | PCM 生产者 | 释放 `audio_queue_mutex_` | 释放 |
| ⑤ | `AbortPcmPlayback()` | `pcm_stream_generation_.fetch_add(1)` | **不持有** |

准确表述（严格按"该帧是否已通过最终令牌检查"划分，而不是按"入队动作是否已完成"划分）：

- **尚未通过最终令牌检查的帧**：Abort 之后**不得入队**。阻塞在 `audio_queue_cv_` 上的生产者
  被 `notify_all()` 唤醒后重查令牌失配，直接返回 `false`；仍在 `pcm_mutex_` 内做字节切片的写入者，
  会在下一次入队检查处失败。
- **已通过最终检查的帧**（上表步骤 ②→③ 之间）：允许完成入队，
  **即使 `push_back` 的实际执行时刻晚于 Abort**。步骤 ③ 已完成的入队无法撤销。
- 取消之后不再允许**新的**帧通过检查；已入队帧自然播完；
  `AbortPcmPlayback()` 不要求清理队列。
- 不增加队列来源标记、不改变 `AudioTask` 语义、不新增锁或播放队列。
- 上述规则**不**提供"Abort 返回时已无帧会被入队"的保证，也**不**提供"取消即静音"（§6.4.5）；
  残留上界仍是 3 帧 ≈ 180 ms（§6.4.3），路线 B 不变。

#### 6.4.3 取消后的残留上界

| 位置 | 上限 | 依据 |
| --- | --- | --- |
| `audio_playback_queue_` 中等待播放 | 2 帧 | `MAX_PLAYBACK_TASKS_IN_QUEUE = 2`（`audio_service.h:40-46`，队列成员在 193 行） |
| `AudioOutputTask` 已 `pop_front()` 并正在 `OutputData()` | 1 帧 | pop 后立即 `output_in_flight_ = true` 并释放锁（`audio_service.cc:335-339`） |
| 正在通过检查并执行 `push_back` 的那一帧 | 0 帧（**已计入队列容量**） | 不得在队列容量之外重复计算 |
| 合计 | **3 帧 ≈ 180 ms** | 每帧 60 ms |

这是**软取消的固有语义**，不是实现缺陷。判断是否需要更强效果由上层的 `Stop()` /
`ResetDecoder()` 决定，其代价是同时打断 Opus。

#### 6.4.4 两套代际的失效路径（分别说明，不得混用残留上界）

| 触发 | 递增的代际 | 对当前 PCM 生产者 | 已入队 PCM 帧 | 残留上界 |
| --- | --- | --- | --- | --- |
| `AbortPcmPlayback()` | **仅** `pcm_stream_generation_` | PCM 令牌失配 → `WritePcmData()` 返回 `false`；在 `audio_queue_cv_` 上阻塞者被 `notify_all()` 唤醒后同样返回 `false` | 保留，自然播完 | 3 帧 ≈ 180 ms |
| `Stop()`（`audio_service.cc:173-193`） | `playback_generation_`（并且 `service_stopped_ = true`，`clear()` 含 `audio_playback_queue_`） | 播放令牌失配（同时 `service_stopped_` 为真）→ 写入返回 `false` | **被清空丢弃** | 1 帧 ≈ 60 ms（仅 in-flight；`AudioOutputTask` 下一轮看到 `service_stopped_` 即退出） |
| `ResetDecoder()`（`audio_service.cc:802-822`） | `playback_generation_`（并 `clear()` `timestamp_queue_`/`audio_decode_queue_`/`audio_playback_queue_`/`audio_testing_queue_`；`service_stopped_` 不变） | 播放令牌失配 → 写入返回 `false`；服务未停止，可重新 `BeginPcmPlayback()` | **被清空丢弃** | 1 帧 ≈ 60 ms |

- 上述两行是**实际源码行为**（`Stop()` 与 `ResetDecoder()` 都持有 `audio_queue_mutex_`、
  都 `++playback_generation_`、都 `clear()` 共享播放队列），不是假定。
- `Stop()` 之后 `service_stopped_` 为真，PCM 写入一律返回 `false`；`ResetDecoder()`
  不清 `service_stopped_`，属于解码器级重置，服务继续运行。
- **PCM Abort 与全局 Stop 的残留上界不同（3 帧 vs 1 帧），不得混为一谈。**

#### 6.4.5 本设计不承诺的后果

- 不承诺取消后立即静音；
- 不承诺撤回已入队帧；
- 不为实现上述效果引入来源标记、新队列或新锁；
- 需要立即停止整个播放链路时，由上层按实际需要调用 `Stop()` / `ResetDecoder()`，
  并接受其对 Opus 的影响（清空解码/播放队列、重置 Opus 解码器）。

### 6.5 输出使能

`BeginPcmPlayback()` 中镜像 `PlaySound()`（`audio_service.cc:769-773`）的做法：

```cpp
if (!codec_->output_enabled()) {
    esp_timer_stop(audio_power_timer_);
    esp_timer_start_periodic(audio_power_timer_, AUDIO_POWER_CHECK_INTERVAL_MS * 1000);
    codec_->EnableOutput(true);
}
```

`AudioOutputTask()` 内部也有同样的兜底（`audio_service.cc:341-345`），
两处并存不会重复生效（`EnableOutput()` 在 `audio_codec.cc:61-67` 里对相同值直接返回）。
提前使能的目的：避免第一帧写入时功放尚未上电导致开头吞音；
`AUDIO_POWER_TIMEOUT_MS`（15 s）超时后由 `CheckAndUpdateAudioPowerState()`
（`audio_service.cc:837-857`）自动关断，语义与 Opus 路径完全相同。

## 7. HTTP 分块接收处理

### 7.1 字节级暂存，而不是样本级

HTTP 返回的是字节流，chunk 边界与 s16 样本边界无关（可能奇数长度、可能在样本中间截断）。
因此暂存单位必须是 **byte**：

- `pcm_pending_bytes_`：`std::vector<uint8_t>`，`reserve(PCM_PLAYBACK_CHUNK_BYTES)`。
- 每次写满 2880 B 就 `memcpy` 成 1440 个 `int16_t`（按样本数计，先保证长度为偶数），
  避免对 `uint8_t*` 做 `reinterpret_cast<int16_t*>` 带来的对齐/别名问题。
- 只有"完整样本"才进入帧化阶段，因此流中间的奇数尾字节**不会**造成样本错位。

### 7.2 `WritePcmData()` 伪代码

```cpp
bool AudioService::WritePcmData(const uint8_t* data, size_t size, bool last) {
    if (service_stopped_.load()) return false;
    if (data == nullptr && size != 0) return false;

    std::lock_guard<std::mutex> lock(pcm_mutex_);
    // 顺序很重要：**先判 active，再处理空块**。
    // 否则 size == 0 && !last 会在 active 检查之前直接返回 true，
    // 与"未调用 BeginPcmPlayback() 不允许写入"的接口约定冲突（§3.3）。
    if (!pcm_stream_active_) {
        ESP_LOGW(TAG, "PCM playback is not active, dropping %u bytes", (unsigned)size);
        return false;
    }
    if (size == 0) {
        if (!last) return true;   // 会话有效时的空块 no-op
        // last == true 的空块等价于"刷尾帧"：把暂存的残余完整样本推完
        return FlushPcmPendingBytes(true, pcm_session_pcm_gen_, pcm_session_playback_gen_);
    }

    // 固定会话令牌：本次调用只使用 BeginPcmPlayback() 建立的快照，
    // 不重新 load 当前代际去"替换"令牌，也不把两个令牌相互比较（§6.2.1）。
    const uint32_t pcm_gen      = pcm_session_pcm_gen_;
    const uint32_t playback_gen = pcm_session_playback_gen_;

    bool framed = false;   // 本次调用是否至少帧化过一次（即已走过权威取消检查）
    size_t offset = 0;
    while (offset < size) {
        const size_t room = PCM_PLAYBACK_CHUNK_BYTES - pcm_pending_bytes_.size();
        const size_t take = std::min(room, size - offset);
        pcm_pending_bytes_.insert(pcm_pending_bytes_.end(), data + offset, data + offset + take);
        offset += take;

        if (pcm_pending_bytes_.size() == PCM_PLAYBACK_CHUNK_BYTES) {
            if (!FlushPcmPendingBytes(false, pcm_gen, playback_gen)) return false;
            framed = true;
        }
    }

    if (last) {
        // 尾帧：完整样本全部推送（长度可 < 1440 samples）；若只剩 1 个孤立字节，
        // 由 FlushPcmPendingBytes 丢弃并告警
        return FlushPcmPendingBytes(true, pcm_gen, playback_gen);
    }

    // 短块补检：本次调用没有触发任何帧化，数据全部留在 pcm_pending_bytes_ 里，
    // 因此没有经过 PushPcmTaskToPlaybackQueue() 的权威检查。
    // 这里用**会话固定令牌**补一次检查，使"Abort 之后的短块写入"立即返回 false，
    // 而不是静默返回 true（否则上层会继续从一个已取消的会话里读 HTTP）。
    // 锁序不变：仍为 pcm_mutex_ -> audio_queue_mutex_。这次获取是短暂且无条件的，
    // 不会等待 audio_queue_cv_（不阻塞背压），也不会反向去取 pcm_mutex_。
    if (!framed) {
        std::lock_guard<std::mutex> queue_lock(audio_queue_mutex_);
        if (service_stopped_.load()) return false;
        if (pcm_gen      != pcm_stream_generation_.load()) return false;  // 只与 PCM 代际比
        if (playback_gen != playback_generation_) return false;           // 只与播放代际比
    }
    return true;
}
```

说明：

- **不整块拷贝 HTTP chunk**。`while` 循环按 `room` 切片，
  所以即使上层一次 `read()` 拿到 8 KB，`pcm_pending_bytes_` 也严格 ≤ 2880 B（需求 10）。
- 跨块奇数长度（例如 1001 B + 1001 B）不会丢字节：第 1 块末尾的单字节留在
  `pcm_pending_bytes_`，第 2 块到达时自然与它的首字节拼成同一个样本。
- 只有在 `last == true` 时才会出现"多余 1 字节"的情况；此时该字节从未参与过
  任何样本的构成，丢弃它不会让后续样本位移（后续已经没有样本了）。
- 循环内**不再重复比较代际**：令牌失效由 `FlushPcmPendingBytes()` → `PushPcmTaskToPlaybackQueue()`
  在 `audio_queue_mutex_` 临界区内统一判定并返回 `false`（§6.2.2），
  避免了"在锁外读一次代际做预判"这种非权威检查。
- **空块与 active 的先后顺序（已修正）**：`size == 0 && !last` 不再在 `active` 检查之前返回
  `true`。现在的顺序是"`service_stopped_` → `pcm_stream_active_` → 空块 no-op"，
  因此未 `BeginPcmPlayback()` 的空块写入同样返回 `false`，与 §3.3 的"未 Begin 不允许写入"一致。
- **短块补检（新增）**：若本次调用没有凑满 2880 B、也就没有触发任何帧化，
  则数据只留在 `pcm_pending_bytes_` 中，没有经过入队处的权威检查。
  此时在返回前用**会话固定令牌**补做一次检查（短暂获取 `audio_queue_mutex_`、不等待 CV），
  使 `AbortPcmPlayback()` 之后的短块写入立即返回 `false`，
  而不是让上层误以为还能继续从 HTTP 拉流。补检**不替换令牌**：
  比较的是 `pcm_session_pcm_gen_` / `pcm_session_playback_gen_` 与当前代际，
  不存在"重新读取当前代际当作令牌"的问题。
- **`WritePcmData()` 与 `AbortPcmPlayback()` 并发的返回语义（明确不承诺无窗口）**：
  允许并发，且**不**保证"Abort 与写入返回之间不存在任何并发窗口"。具体地说，
  `WritePcmData()` 返回 `true` 只表示"该批字节已被接受（已入暂存或已成功入队）"，
  **不表示** Abort 尚未发生 —— 极端情况下 Abort 可能在本次调用最后一次检查之后、
  函数返回之前生效，此时本次调用仍返回 `true`，但后续写入必然返回 `false`。
  上层不得用 `true` 推断会话仍有效，唯一权威依据是后续调用的返回值。
- `FlushPcmPendingBytes()` 内部会调用 `PushPcmTaskToPlaybackQueue()`，
  后者在播放队列满时阻塞 —— 这就是背压点（见 §8）。
  由于该阻塞发生在 `pcm_mutex_` 之内，`AbortPcmPlayback()` 用
  `pcm_stream_generation_` 原子自增 + `notify_all()` 破除等待（§6.3、§6.4）。

### 7.3 `FlushPcmPendingBytes()` 伪代码

```cpp
bool AudioService::FlushPcmPendingBytes(bool is_final,
                                        uint32_t pcm_generation,
                                        uint32_t playback_generation) {
    // 调用方必须持有 pcm_mutex_；两套令牌全部由参数传入
    for (;;) {
        if (pcm_pending_bytes_.size() < sizeof(int16_t)) break;
        size_t byte_count = pcm_pending_bytes_.size() - (pcm_pending_bytes_.size() % sizeof(int16_t));
        if (byte_count > PCM_PLAYBACK_CHUNK_BYTES) byte_count = PCM_PLAYBACK_CHUNK_BYTES;
        // is_final == false 时只推送整帧；is_final == true 时允许把余量作为尾帧推送
        if (!is_final && byte_count < PCM_PLAYBACK_CHUNK_BYTES) break;

        const uint32_t frame_samples = (uint32_t)(byte_count / sizeof(int16_t));
        std::vector<int16_t> frame(frame_samples);
        std::memcpy(frame.data(), pcm_pending_bytes_.data(), byte_count);
        pcm_pending_bytes_.erase(pcm_pending_bytes_.begin(),
                                 pcm_pending_bytes_.begin() + byte_count);

        // 令牌原样透传：本函数内部不读 pcm_stream_generation_ / playback_generation_
        if (!PushPcmTaskToPlaybackQueue(std::move(frame), pcm_generation, playback_generation)) {
            return false;                                   // 取消/停止：不推进媒体时间
        }
        // 只有成功入队后才推进媒体时间，整数毫秒向下取整
        pcm_media_position_ms_ += frame_samples * 1000u / 24000u;   // 1440 -> 60；尾帧 -> 实际时长
    }
    if (is_final && !pcm_pending_bytes_.empty()) {
        // 只剩 1 个字节，永远无法构成一个样本：丢弃并告警（不会造成任何样本位移）
        ESP_LOGW(TAG, "Discarding 1 trailing byte at end of PCM stream");
        pcm_pending_bytes_.clear();
    }
    return true;
}
```

- **令牌完全参数化**：函数体内部不存在任何 `pcm_stream_generation_.load()` 或
  `playback_generation_` 读取，因此不会出现"函数内部重新取当前代际替换会话快照"的错误。
- **媒体时间只在成功入队后推进**：`task.media_position_ms` 由 `PushPcmTaskToPlaybackQueue()`
  取入队前的 `pcm_media_position_ms_`；推进发生在 `push` 返回 `true` 之后。
- 完整帧推进 60 ms；尾帧按 `frame_samples * 1000 / 24000` 推进，可小于 60 ms，
  因此最后一帧的时间戳不严格对齐 60 ms 栅格（允许）。
  式中的除数 24000 就是 `codec_->output_sample_rate()`：§5.1 已在会话开始时校验二者相等，
  因此常量与运行时值不会分歧（若将来换板卡，校验会先失败）。

尾字节处理的完整语义：

- **流中间**：奇数尾字节始终留在 `pcm_pending_bytes_`，下一个 chunk 到达时与它的首字节
  拼成同一个样本，零丢失、零错位（1001 B + 1001 B 的极端情况由 §7.2 的 `while` 切片自然覆盖）。
- **流结尾**（`is_final == true`）：先把所有完整样本推完（含不足 1440 samples 的尾帧，
  保证 TTS 尾音不被截断），若最后仅剩 1 个字节则丢弃并 `ESP_LOGW`。
- 该字节从未参与过任何样本构成，因此丢弃它不会让已推送样本发生位移，符合"不丢失或错位 PCM 样本"的要求。

## 8. 缓冲区容量与背压策略

### 8.1 容量上界与数据缓冲上界之和

**各结构自身的容量上界**

| 层级 | 数据结构 | 容量上界 | 满时行为 |
| --- | --- | --- | --- |
| HTTP 输入 | 调用方 `read()` 缓冲 | 调用方决定（建议 ≤ 4 KB） | 本设计不缓存完整 body，边收边写 |
| 字节暂存 | `pcm_pending_bytes_` | **2880 B**（硬上界，与 chunk 大小无关） | 达到 2880 B 立即帧化并清空 |
| 播放队列 | `audio_playback_queue_` | 2 帧 = 5760 B（与 Opus 共享） | 生产者阻塞在 `audio_queue_cv_` |

**各缓冲独立上界之和（保守估算，不等于同一瞬间的峰值）**

下表把各缓冲区**各自独立**的容量上界相加，得到**上界之和**
14400 B ≈ 14.4 KB。它既**不是**"这几块数据必然会在同一瞬间同时达到各自峰值"的断言，
也**不是**本功能的总内存占用。

| 缓冲位置 | 独立上界 |
| --- | --- |
| `pcm_pending_bytes_` | 2880 B |
| 队列中待播放帧（容量 2） | 2 × 2880 B = 5760 B |
| `AudioOutputTask` 已取出、正在播放的帧 | 2880 B |
| PCM 生产者已生成、等待入队的临时帧 | 2880 B |
| **上界之和（保守估算）** | **14400 B ≈ 14.4 KB** |

说明（逐条对应前述口径）：

1. 该"合计"是**独立上界之和**，不是同一瞬间必然可达的峰值，更不是全链路内存占用。
2. `pcm_pending_bytes_` 与"等待入队的临时帧"**并非必然同时达到最大值**：
   `FlushPcmPendingBytes()` 先把 `byte_count` 字节 `memcpy` 进临时帧、**随后**才从
   `pcm_pending_bytes_` 擦除，因此 `pcm_pending_bytes_.size() == 2880` 与"临时帧 2880 B"
   只在"拷贝完成 → 擦除完成"这段极短窗口内并存，擦除后暂存立即归零。
3. 入队阻塞期间（在 `audio_queue_cv_` 上等待）生产者持有 `pcm_mutex_`，无法继续填充暂存；
   此时实际共存量约为 2880（临时帧）+ 5760（队列 2 帧）+ 2880（in-flight）
   = **11520 B ≈ 11.3 KB**，`pcm_pending_bytes_` 通常为空。
4. 因此运行时观测到的稳态峰值应明显低于 14400 B；14400 B 只作为"上界、不可越过"使用。
5. 不包含：`std::vector` 的容量冗余（`pcm_pending_bytes_` 的 `reserve(2880)` 会长期占用
   这份容量）、`AudioTask` 等对象开销、任务栈、互斥量/条件变量、日志缓冲及其它运行时内存。
6. 临时帧与队列中的帧是**两个独立对象**：`push_back(std::move(task))` 完成所有权转移之前，
   临时帧仍占用自己的 2880 B，不能与队列帧合并计算；转移完成后临时对象只是被 move 过的空壳。
7. 旧版文档中的 8.4 KB 仅是"字节暂存 + 队列"两项之和（2880 + 5760），
   **不得**再被描述为峰值，更不得当作全链路内存占用。
8. 14.4 KB **不得**被描述为整个功能的总内存占用；峰值也不随播放时长增长：
   长时间 TTS（数分钟）不会累积内存（需求 10），且本设计不含重采样器状态与 scratch 缓冲。

### 8.2 背压链路

```text
HTTP socket recv
  -> WritePcmData()                      填 pcm_pending_bytes_（满 2880 B 立即刷）
  -> FlushPcmPendingBytes()              字节 -> int16_t 帧化（memcpy）
  -> PushPcmTaskToPlaybackQueue()        队列满 -> 阻塞在 audio_queue_cv_
  -> (AudioOutputTask 每帧写入 I2S DMA 后 notify_all，生产者被唤醒)
```

- 阻塞点固定在 `PushPcmTaskToPlaybackQueue()` 的 `audio_queue_cv_.wait(...)`，
  谓词与 `OpusCodecTask()`（`audio_service.cc:384-388`）相同。
- 播放队列被喂满 ⇒ HTTP 读取循环变慢 ⇒ TCP 接收窗口收窄 ⇒ 百度 TTS 服务器自然降速。
  整条链路没有"第二个无界队列"，天然具备端到端背压。
- **明确不做丢帧**。麦克风路径满时会丢最旧帧（`audio_service.cc:585-592`），因为实时音频
  迟到即无价值；TTS PCM 是点播音源，丢帧会产生可听见的空洞，因此只能阻塞。

### 8.3 上层调用约束

- 必须"边收边写"：禁止把整个 HTTP body 读进 `std::string`/`std::vector` 再一次性写入，
  否则无界内存出现在调用方，本设计无法兜底。
- 写入线程不得是主事件循环线程（`Application`）。按 `AGENTS.md` 要求，
  应使用 `Application::Schedule()` 或独立的 HTTP 接收任务驱动，
  避免 `PushPcmTaskToPlaybackQueue()` 的阻塞影响协议与状态机。
- `EndPcmPlayback()` 可能阻塞等待排空（最长 `PCM_PLAYBACK_DRAIN_TIMEOUT_MS`；
  若等待期间被 Abort/Stop/ResetDecoder 取消则立即返回，§9.1），
  同样不得在音频任务或 `esp_timer` 回调中调用。
- `BeginPcmPlayback()` / `EndPcmPlayback()` / `AbortPcmPlayback()` 由同一控制线程串行调用，
  只有 `WritePcmData()` 可以与 `AbortPcmPlayback()` 并发（§6.3）。

## 9. 播放完成、队列排空与错误退出

### 9.1 `EndPcmPlayback()` 流程

```cpp
bool AudioService::EndPcmPlayback(uint32_t timeout_ms) {
    uint32_t pcm_gen      = 0;
    uint32_t playback_gen = 0;
    bool flush_ok = true;
    {
        std::lock_guard<std::mutex> lock(pcm_mutex_);
        // 无会话：只报告播放链路是否空闲，不改任何状态
        if (!pcm_stream_active_) return IsPlaybackIdle();

        // 保存本次会话**固定**的 PCM 令牌：等待排空期间用它识别取消，
        // 绝不重新读取当前代际来替换会话令牌（§6.2.1）。
        pcm_gen      = pcm_session_pcm_gen_;
        playback_gen = pcm_session_playback_gen_;

        // 尾帧（可 < 1440 samples）入队：与 WritePcmData() 使用完全相同的令牌
        flush_ok = FlushPcmPendingBytes(true, pcm_gen, playback_gen);

        // 无论 flush 成功与否都关闭"可写"：避免出现"令牌已失效但 active 长期为真"的歧义状态
        pcm_stream_active_ = false;
    }
    if (!flush_ok) return false;      // 尾帧入队时已失效（Abort/Stop/ResetDecoder）：立即返回，不等待

    std::unique_lock<std::mutex> lock(audio_queue_mutex_);
    audio_queue_cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&] {
        return service_stopped_.load() ||                          // 服务停止
               pcm_gen      != pcm_stream_generation_.load() ||    // 等待期间发生 Abort
               playback_gen != playback_generation_ ||             // 等待期间发生 Stop/ResetDecoder
               IsPlaybackDrainedLocked();                          // 正常排空
    });

    // 退出后按"取消优先、其次排空、最后超时"的顺序判定；三项都在同一个临界区内读取，
    // 与谓词判定保持一致（pcm_stream_generation_ 是 atomic，不涉及反向取 pcm_mutex_）。
    if (service_stopped_.load()) return false;
    if (pcm_gen      != pcm_stream_generation_.load()) return false;
    if (playback_gen != playback_generation_) return false;
    return IsPlaybackDrainedLocked();              // 已排空 -> true；仅超时且未排空 -> false
}
```

**三条返回路径（互斥且完备）**

| 路径 | 触发条件 | 返回值 | `pcm_stream_active_` |
| --- | --- | --- | --- |
| ① 正常排空 | 谓词因 `IsPlaybackDrainedLocked()` 成立而返回，且令牌未失效 | `true` | `false`（已关闭） |
| ② 等待期间被取消 | `service_stopped_` 置位，或 `pcm_gen` / `playback_gen` 失配（Abort / Stop / ResetDecoder） | `false`，**立即返回**，不等满 `timeout_ms` | `false` |
| ③ 仅等待超时 | `wait_for` 超时，令牌仍有效但队列未排空 | `false`，不清队列、不 `ResetDecoder()` | `false` |

- 路径 ② 解决了"End 进入排空等待之后才发生 Abort，却仍要等满 `timeout_ms`"的问题：
  排空谓词**同时**检查 PCM 令牌与播放令牌，取消一旦发生就立刻唤醒并返回 `false`。
  `AbortPcmPlayback()` 末尾的 `notify_all()`（§6.3）负责唤醒。
- 等待期间**只持有 `audio_queue_mutex_`**：谓词里对 `pcm_stream_generation_` 只做原子读，
  **不会**去取 `pcm_mutex_`，因此不存在 `audio_queue_mutex_` → `pcm_mutex_` 的反向获取，
  锁序仍是 `pcm_mutex_ → audio_queue_mutex_`，且与 Abort 的"先原子自增、后取 `pcm_mutex_`"
  顺序可以安全交错（不会互相卡死）。
- 排空判定**完全复用** `IsPlaybackDrainedLocked()`（`audio_service.cc:824-827`）：
  `audio_decode_queue_` 空 && `audio_playback_queue_` 空 && `!decode_in_flight_` && `!output_in_flight_`。
  最后这一项 `output_in_flight_` 保证"最后一帧真的写进 I2S 之后"才算完成，
  不会提前返回导致上层关闭功放。
- `wait_for` 的超时语义（路径 ③）：超时返回 `false` 但**不清队列、不 `ResetDecoder()`**。
  PCM 帧会继续自然播完，避免误伤同一队列中的 Opus 音频。
- 该排空判定是**全局**的（同时覆盖 PCM 与 Opus），因此 `EndPcmPlayback()` 的返回时间
  取决于队列中是否还有无关音频 —— 这正是 §9.5 独占播放契约存在的原因。
- `flush_ok == false` 表示尾帧入队时令牌已失效（会话被取消），此时直接返回 `false`，
  不进入等待，避免取消后仍然阻塞 `timeout_ms`。
- **三种失败路径（②③ 以及 `flush_ok == false`）都不会留下"代际已失效但 `active` 为真"的状态**：
  `pcm_stream_active_` 在离开 `pcm_mutex_` 之前就已置 `false`（§9.4）。

### 9.2 完成事件

PCM 的最后一帧入队时已把 `playback_drained_notified_ = false`（§6.2），
`AudioOutputTask()` 写完后调用 `MarkPlaybackDrainedLocked()`（`audio_service.cc:369`）
并触发 `callbacks_.on_playback_drained()`（`audio_service.cc:373-375`）。
因此上层既可以用 `EndPcmPlayback()` 同步等待，也可以完全依赖既有回调，
**不需要新增任何回调字段**（`AudioServiceCallbacks` 结构体保持不变）。

### 9.3 取消与残留上界

| 场景 | 处理 | 递增的代际 | 已入队 PCM 帧 | 残留上界 |
| --- | --- | --- | --- | --- |
| 用户按键打断 / 新唤醒 / 上层主动取消 | `AbortPcmPlayback()` | **仅** `pcm_stream_generation_` | 自然播完（不主动清理） | 3 帧 ≈ 180 ms（§6.4.3） |
| `AudioService::Stop()` | 既有逻辑（`audio_service.cc:173-193`）：置 `service_stopped_`、`++playback_generation_`、`clear()` 含 `audio_playback_queue_` 的全部队列 | `playback_generation_` | 全部丢弃 | 1 帧 ≈ 60 ms（仅 in-flight） |
| `ResetDecoder()` | 既有逻辑（`audio_service.cc:802-822`）：`++playback_generation_`、`clear()` `audio_playback_queue_` 等队列、重置 Opus 解码器；不清 `service_stopped_` | `playback_generation_` | 全部丢弃 | 1 帧 ≈ 60 ms（仅 in-flight） |

- 以上三种路径都属于**软取消**：取消之后不再有**新的**帧通过最终令牌检查；
  已通过最终检查的帧可能仍完成入队（§6.4.2），它们是否播放取决于谁清理了队列
  （Abort 不清理，Stop/ResetDecoder 清理）。
- **PCM Abort 与全局 Stop/ResetDecoder 的残留上界不同（3 帧 vs 1 帧），不得混为一谈。**
- 硬打断（立刻静音）不在本设计范围内：共用队列时"只清 PCM 帧"需要遍历队列并识别帧来源，
  会污染 `AudioTask` 语义。需要硬打断时，由上层复用既有的
  `Stop()` / `ResetDecoder()`（会同时影响 Opus，属可接受的上层决策）。
- `AbortPcmPlayback()` 的锁序要求见 §6.3，是唯一需要特别注意的实现细节；
  它**不得**触碰 `playback_generation_`。
- 三条路径都**不承诺**"取消即静音"（§6.4.5）。

### 9.4 错误退出

| 失败点 | 行为 |
| --- | --- |
| `BeginPcmPlayback()` 参数非法（采样率/声道不符） | 返回 `false` + `ESP_LOGE`，状态不变 |
| `WritePcmData()` 时 `!pcm_stream_active_` | 返回 `false`，只记一次 `ESP_LOGW` |
| `PushPcmTaskToPlaybackQueue()` 令牌失配（取消/停止） | 返回 `false`，不推进媒体时间，`WritePcmData()` 返回 `false` |
| `PushPcmTaskToPlaybackQueue()` 中 `push_back()` 返回 `false` | 记 `ESP_LOGE`，返回 `false`（理论上不可达，防御性处理） |
| `service_stopped_` | 所有写入立即 `false` |
| `EndPcmPlayback()` 尾帧入队时令牌已失效（Abort/Stop/ResetDecoder） | 返回 `false`（不进入排空等待）；`pcm_stream_active_` 已置 `false` |
| `EndPcmPlayback()` 在排空等待期间被取消（路径 ②） | 返回 `false`（立即返回，不等满超时）；`pcm_stream_active_` 已置 `false` |
| `EndPcmPlayback()` 仅在等待超时（路径 ③） | 返回 `false`；不清队列、不 `ResetDecoder()`；`pcm_stream_active_` 已置 `false` |
| 底层 I2S 写失败 | **无法上报**：`AudioCodec::OutputData()` 返回 `void`（`audio_codec.cc:17-19`），与 Opus 路径的既有行为一致 |

**失败后的会话状态约定（唯一口径）**

- 只要 `EndPcmPlayback()` 被调用过（无论返回 `true` 还是 `false`，也无论走哪条路径），
  返回时 **`pcm_stream_active_` 一定为 `false`**：`EndPcmPlayback()` 在离开 `pcm_mutex_` 之前
  就关闭了"可写"标志（§9.1）。因此不存在"代际已失效但会话仍显示 active"的长期歧义。
- 因此**不要求**调用方在 `EndPcmPlayback()` 失败后再调用 `AbortPcmPlayback()`；
  但 `AbortPcmPlayback()` 是幂等的，调用它也不会有副作用（重复清理暂存、重复清零标志）。
- `AbortPcmPlayback()` 本身也会把 `pcm_stream_active_` 置 `false`；`Stop()` / `ResetDecoder()`
  属于全局操作，**不**会动该标志（它们只让令牌失效，§6.4.4）。
- `IsPcmPlaybackActive()` 只返回 `pcm_stream_active_`，因此：
  - `BeginPcmPlayback()` 之后、`EndPcmPlayback()` / `AbortPcmPlayback()` 之前 → `true`；
  - `Stop()` / `ResetDecoder()` 之后、`EndPcmPlayback()` 之前 → **仍可能返回 `true`**，
    但此时写入一定失败（播放令牌失配）。这一短暂不一致是刻意保留的，
    目的不是让上层据此判断可写性：**判断可写性的唯一权威依据是
    `WritePcmData()` / `EndPcmPlayback()` 的返回值**（§3.1）。
  - `EndPcmPlayback()` 任一路径返回之后 → `false`。

约定：`WritePcmData()` 返回 `false` 后，调用方必须停止推送并关闭 HTTP 连接；
`EndPcmPlayback()` 返回 `false` 后同样必须关闭连接、丢弃后续数据。
`AbortPcmPlayback()` 只是"清理"动作（幂等、可选），不是必须步骤；
`AudioService` 内部不做自动重试、不做自动重连。

### 9.5 PCM 会话独占播放契约（上层仲裁）

**契约：PCM 播放会话存续期间（`BeginPcmPlayback()` 成功之后，到 `EndPcmPlayback()`
返回或 `AbortPcmPlayback()` 生效为止），`Application` 状态机不得主动发起新的 Opus TTS 播放；
PCM 会话正常结束或取消之后，才允许恢复 Opus 播放。**

原因：`EndPcmPlayback()` 依赖的 `IsPlaybackDrainedLocked()`（`audio_service.cc:824-827`）
是**全局**排空判定，同时涉及 `audio_decode_queue_`、`audio_playback_queue_`、
`decode_in_flight_`、`output_in_flight_`，**不区分帧来源**。若 PCM 会话结束阶段仍有
无关的 Opus 音频进入播放队列（或持续产生新的 Opus 帧），`EndPcmPlayback()` 会连带等待
那些无关音频，返回时间不可预测，甚至超时。共用的 `AudioTask` 没有来源标记，
`AudioService` 在结构上无法区分两种来源，因此只能由上层仲裁。

本轮**只确认契约，不修改**：

- `AudioTask` / `AudioTaskType` 语义；
- 共用播放队列 `audio_playback_queue_`；
- 既有 Opus 播放架构（解码、`output_resampler_`、drain 回调）。

后续 `Application` 接入时的**验收要求**：

1. 在 `Application` 状态机中增加显式的"PCM 会话进行中"判断
   （状态变更仍走 `Application::SetDeviceState()`）。
2. 在发起任何 Opus TTS 播放之前检查该状态；PCM 会话进行中则不得把 Opus 帧入队
   （延后、排队或拒绝由上层决定，但必须显式决策）。
3. **不能仅依赖 `AudioService` 内部状态**（如 `IsPcmPlaybackActive()`）做该仲裁：
   `AudioService` 不感知 `Application` 的播放意图，也不知道协议层即将推入 Opus 帧。
4. 若在 PCM 会话进行中收到 TTS 响应，应视为上层逻辑错误并记录日志，
   禁止静默交错播放（§10.3）。
5. PCM 会话结束后，依据 `EndPcmPlayback()` 的返回值或既有 `on_playback_drained`
   回调恢复 Opus 播放能力。

违反契约的后果是 `EndPcmPlayback()` 返回时间不可预测、PCM 尾音可能与 Opus 交错；
这属于上层违约，不是 `AudioService` 的缺陷。

## 10. 如何确保原有 Opus 播放功能不受影响

### 10.1 完全未改动的代码（逐项清单）

| 代码 | 位置 | 是否改动 |
| --- | --- | --- |
| `OpusCodecTask()` 全文 | `audio_service.cc:381-530` | 不改 |
| `PushPacketToDecodeQueue()` | `audio_service.cc:609-629` | 不改 |
| `SetDecodeSampleRate()` / `output_resampler_` | `audio_service.cc:532-567` | 不改（PCM 路径不涉及重采样） |
| `PlaySound()`（Ogg + `OggDemuxer`） | `audio_service.cc:768-790` | 不改 |
| `ResetDecoder()` | `audio_service.cc:802-822` | 不改 |
| `AudioOutputTask()` | `audio_service.cc:326-379` | 不改 |
| `IsPlaybackDrainedLocked()` / `MarkPlaybackDrainedLocked()` | `audio_service.cc:824-835` | 不改 |
| `AudioServiceCallbacks` 结构体 | `audio_service.h:82-90` | 不改（不新增回调） |
| `AudioTaskType` / `AudioTask` | `audio_service.h:92-104` | 不改（复用既有枚举值） |
| `MAX_PLAYBACK_TASKS_IN_QUEUE` / 其它队列容量宏 | `audio_service.h:40-46` | 不改 |
| `audio_decode_queue_` / `audio_encode_queue_` / `audio_send_queue_` / `audio_testing_queue_` | `audio_service.h:188-193` | 不改 |
| `AudioCodec::OutputData()` / `Write()` / `Start()` | `audio_codec.cc` 全文 | 不改 |
| `no_audio_codec.cc` | - | 不改（需求 5） |
| I2S 初始化/写驱动 | 各 board / `audio_codec_*.cc` | 不改（需求 6） |
| `AudioInputTask()` / `ReadAudioData()` / `input_resampler_` | `audio_service.cc:195-324` | 不改（需求 4） |
| AFE / WakeNet / `audio_engine_*` | `main/audio/engines/**` | 不改 |
| `fixed_queue.h` | `main/audio/fixed_queue.h` | 不改 |
| `esp_ae_rate_cvt` 组件 | `managed_components/**` | 不改（仍只被 `SetDecodeSampleRate()` 以原样方式使用） |

唯一被修改的两个文件只有 `audio_service.h`（新增声明/成员/宏）与 `audio_service.cc`
（新增 7 个函数实现），**全部为追加，无一处修改既有语句**；连析构函数也不需要改动
（PCM 路径不再持有任何需要释放的句柄）。

### 10.2 唯一共享面与安全性论证

| 共享对象 | 影响 | 论证 |
| --- | --- | --- |
| `audio_playback_queue_` | 多一个生产者 | 容量仍是 2；`FixedQueue::push_back()` 满时返回 `false`（`fixed_queue.h:39-46`），生产者只会阻塞不会覆盖；`AudioOutputTask()` 的 `front()`/`pop_front()` 语义不变 |
| `audio_queue_mutex_` / `audio_queue_cv_` | 临界区被 PCM 生产者短暂使用 | PCM 只在"令牌检查 + `push_back`"瞬间持锁（`BeginPcmPlayback()` 取快照时也短暂持锁），`memcpy` 帧化已在 `pcm_mutex_` 内完成（§6.3） |
| `playback_drained_notified_` | 会被 PCM 置 `false` | 与 `PushPacketToDecodeQueue()`（`audio_service.cc:625`）语义相同，Opus 行为不变 |
| `playback_generation_` | 只读 | PCM 只在 `BeginPcmPlayback()` 中持 `audio_queue_mutex_` 快照一次，从不修改；`Stop()`/`ResetDecoder()` 的自增仍然对 PCM 生效 |
| `debug_statistics_.playback_count` | 统计包含 PCM 帧 | 仅日志口径变化（`audio_service.cc:355`），无逻辑影响 |

### 10.3 与 Opus 同时播放时的行为

- **结构上**允许共存：共用队列按帧 FIFO，若两种来源同时存在，顺序可能是
  `[Opus 60ms][PCM 60ms][Opus 60ms]…` 交错。
- 不会出现并发写 I2S：唯一消费者是 `AudioOutputTask()`（单任务串行执行
  `codec_->OutputData()`，`audio_service.cc:351`）。
- **契约上不允许这种共存**（§9.5）：PCM 会话存续期间 `Application` 不得发起新的
  Opus TTS 播放。原因很具体——`EndPcmPlayback()` 的排空判定是全局的，
  无关 Opus 音频进入队列会让它的返回时间不可预测。
- 因此"交错"只是结构可能性，**不是**受支持的使用方式；`AudioService` 不做来源仲裁、
  不增加来源标记，`Application` 必须保证时序（正常场景：服务端 TTS 先 `AbortSpeaking`
  再播本地 PCM，或等 PCM 会话结束后再播 TTS）。

### 10.4 共存注意事项（实现时容易踩的点）

1. **server AEC timestamp**：`AudioOutputTask()` 在 `CONFIG_USE_SERVER_AEC` 下会把
   `task.timestamp > 0` 的帧时间戳压入 `timestamp_queue_`（`audio_service.cc:359-367`）。
   PCM 帧固定填 `timestamp = 0`，因此不会污染服务端 AEC 的时间戳队列；
   PCM 播放期间服务端下发的 Opus 帧仍按各自 `packet->timestamp` 正常记录。
2. **进度回调**：`on_playback_progress` 由 `task.playback_id != 0` 触发
   （`audio_service.cc:347-349`）。`BeginPcmPlayback(..., playback_id)` 传非 0 时，
   PCM 帧会带 `pcm_media_position_ms_`（入队前的媒体时间，按实际帧长推进：完整帧 +60 ms，
   尾帧 `sample_count * 1000 / 24000`），与 Opus 的 `packet->media_position_ms` 语义一致；
   传 0 则完全静默，避免上层混淆两个播放源。
3. **电源定时器**：`AudioOutputTask()` 与 `CheckAndUpdateAudioPowerState()`
   都会按 `last_output_time_` 判定关断（`AUDIO_POWER_TIMEOUT_MS = 15000`），
   PCM 帧同样刷新 `last_output_time_`（`audio_service.cc:354`），无需额外处理。
4. **不要在 PCM 路径调用 `ResetDecoder()` 或 `PlaySound()`**：前者会
   `++playback_generation_` 并清空共享播放队列（连带丢弃 Opus 帧，`audio_service.cc:802-822`），
   后者会把 PCM 当作 Ogg 解析（`OggDemuxer` 会直接失败）。
5. **PCM 路径不要碰任何重采样器**：本设计不需要重采样（24 kHz 直通），
   `output_resampler_` 仍只由 `SetDecodeSampleRate()` 在 Opus 流采样率变化时 close/open
   （`audio_service.cc:556-565`）。若将来把百度 TTS 改成其它采样率，**不要**在 PCM 路径
   借用 `output_resampler_`（会导致句柄竞态），而应新增独立句柄或改回显式重采样设计。
6. **不要修改 `MAX_PLAYBACK_TASKS_IN_QUEUE`**。若实测 PCM 播放有欠载，
   优先增大分帧时长（60 ms → 最多 100 ms）而不是加深队列；加深队列会增加端到端延迟
   并同时影响 Opus 的打断响应速度。分帧时长的取舍见 §13.6。
7. **不要给 `AudioTask` 加"来源"标记**：一旦加标记，`AudioOutputTask()` 与
   `ResetDecoder()` 的既有语义都要改，违反"不修改 Opus 链路"的前提；
   来源仲裁放在 `Application`（§9.5）。

## 11. 具体修改文件清单

### 11.1 本阶段（架构设计，已完成）

| 文件 | 操作 |
| --- | --- |
| `docs/PCM_PLAYBACK_DESIGN.md` | **新建**（本文件） |

### 11.2 后续实现阶段（本文档不实施，仅供评审）

| 文件 | 操作 | 具体内容 |
| --- | --- | --- |
| `main/audio/audio_service.h` | 追加 | 4 个宏（§4.1）；5 个公有方法声明（§3.1）；2 个私有方法声明（§3.2）；**8 个成员变量**（§4.2：`pcm_pending_bytes_`、`pcm_mutex_`、`pcm_stream_generation_`、`pcm_session_pcm_gen_`、`pcm_session_playback_gen_`、`pcm_stream_active_`、`pcm_playback_id_`、`pcm_media_position_ms_`） |
| `main/audio/audio_service.cc` | 追加 | 7 个新函数实现：`BeginPcmPlayback()`、`WritePcmData()`、`EndPcmPlayback()`、`AbortPcmPlayback()`、`IsPcmPlaybackActive()`（§3.1）+ `PushPcmTaskToPlaybackQueue()`、`FlushPcmPendingBytes()`（§3.2）；析构函数**不需要**改动 |
| `main/audio/audio_service.cc` | 不修改 | 现有 8 个既有函数体一行不动 |

### 11.3 明确不修改的文件

`main/audio/audio_codec.h`、`main/audio/audio_codec.cc`、`main/audio/no_audio_codec.cc`、
`main/audio/fixed_queue.h`、`main/audio/ogg_demuxer.*`、`main/audio/engines/**`、
`main/audio/audio_engine.*`、各 board 的 `audio_codec_*.cc`（I2S 驱动）、
`main/protocols/**`、`main/application.*`、`main/CMakeLists.txt`、`main/Kconfig.projbuild`、
`managed_components/**`、`sdkconfig*`。

> 调用方（HTTP 拉流 + 状态机切换）属独立任务，不在本次修改清单内；
> 其对接方式已在 §8.3 给出约束（边收边写、非主循环线程、`Application::Schedule()`）。

## 12. 验证计划（实现阶段执行）

### 12.1 构建

```sh
python scripts/build.py --list-boards          # 确认目标 board 目录名与 variant
python scripts/build.py <board-directory> --name <variant-name>
python -m unittest discover -s scripts/tests -v
clang-format --dry-run -Werror main/audio/audio_service.h main/audio/audio_service.cc
```

### 12.2 逻辑用例（宿主侧/真机）

| 用例 | 期望 |
| --- | --- |
| 常规 TTS 播放（24 kHz 直通） | 音调、语速、时长与服务器合成结果一致（无变调）、无咔哒声 |
| 帧长校验 | 除尾帧外每帧 `pcm.size() == 1440`（2880 B = 60 ms）；尾帧满足 `0 < pcm.size() < 1440` |
| HTTP chunk = 1000 B 奇数重复 | 无丢样本、无爆音、时长与原 PCM 一致 |
| HTTP chunk = 8192 B | `pcm_pending_bytes_.size()` 始终 ≤ 2880（加断言验证） |
| 缓冲峰值观测 | 各缓冲区独立上界之和 ≤ 14400 B（§8.1）；实测稳态不应同时达到各峰值（阻塞入队期间约 11520 B），且全程不随播放时长增长 |
| 播放中 `AbortPcmPlayback()` | 写入立即返回 `false`，无死锁；最多再响 **3 帧 ≈ 180 ms**；`playback_generation_` 保持不变 |
| `EndPcmPlayback()` 排空 | 最后一帧写完后才返回 `true`；`on_playback_drained` 恰好触发一次；返回后 `IsPcmPlaybackActive()` 为 `false` |
| `EndPcmPlayback()` 等待期间 `AbortPcmPlayback()` | **立即**返回 `false`（不等满 `timeout_ms`），无死锁；`pcm_stream_active_` 为 `false`；无需再额外调用 Abort |
| `EndPcmPlayback()` 等待期间 `Stop()` / `ResetDecoder()` | 立即返回 `false`（播放令牌失配）；队列已被既有逻辑清空 |
| `EndPcmPlayback()` 仅超时 | 返回 `false`；不清队列、不 `ResetDecoder()`；已入队 PCM 帧继续自然播完 |
| `WritePcmData()` 短块 + Abort | 不足 2880 B、本次调用未触发帧化的写入，在 Abort 之后返回 `false`（会话固定令牌补检，§7.2） |
| 未 Begin 的空块写入 | `WritePcmData(nullptr, 0, false)` 在未 `BeginPcmPlayback()` 时返回 `false`（不是 `true`） |
| 控制面串行化 | `Begin`/`End`/`Abort` 由同一线程串行调用；代码与日志中不出现以 `IsPcmPlaybackActive()` 代替返回值的仲裁逻辑（§6.3、§9.4） |
| 播放中 `Stop()` | 写入返回 `false`；播放队列被清空；最多再响 1 帧（in-flight）；音频任务正常退出 |
| 播放中 `ResetDecoder()` | 写入返回 `false`；播放队列被清空；`service_stopped_` 未被置位，可重新 `BeginPcmPlayback()` |
| 双令牌独立性 | `AbortPcmPlayback()` 后仅 PCM 令牌失配、播放令牌仍等于当前 `playback_generation_`；`Stop()`/`ResetDecoder()` 后反之；代码中不存在两令牌交叉比较 |
| 媒体时间 | 完整帧推进 60 ms；尾帧推进 `sample_count * 1000 / 24000`；`on_playback_progress` 单调不减；取消时**不推进** |
| 独占播放契约（`Application` 接入后） | PCM 会话期间收到 TTS 响应不得把 Opus 帧入队，并记录错误日志；会话结束后才恢复 Opus 播放（§9.5） |
| Opus 播放与时序回归 | 服务端 TTS 的 Opus 播放、打断、重连、`on_playback_progress` 与改动前一致 |
| 长时播放（≥ 5 分钟） | 内存不增长（`esp_get_free_heap_size()` 稳定） |
| 采样率不匹配（例如 16000 Hz 请求） | `BeginPcmPlayback()` 返回 `false` + `ESP_LOGE`，不产生任何播放 |
| 欠载（连续 2×60 ms 供帧不足） | 记录日志；按 §13.6 增大分帧时长（上限 100 ms），不加深队列 |

### 12.3 需求对照表

| 需求 | 落实位置 |
| --- | --- |
| 1 不经过 Opus 解码器 | §4.3（帧由原始字节直接构成）、§5.2、§10.1 |
| 2 不经过 `OggDemuxer` | §1（不复用 `PlaySound()`）、§10.4-4 |
| 3 不改变 Opus 链路 | §10.1、§10.2 |
| 4 不改变采集/AFE/WakeNet | §6.3（`pcm_mutex_` 与 `input_resampler_mutex_` 隔离）、§10.1 |
| 5 不修改 `no_audio_codec.cc` | §11.3 |
| 6 不修改 I2S 驱动 | §6.1（只经 `codec_->OutputData()`）、§11.3 |
| 7 ~~重采样器独立~~（已变更：不再需要重采样器） | §5.1（严格采样率校验，拒绝而非转换）、§10.4-5 |
| 8 共用 `AudioOutputTask` | §4.2 说明、§6.1、§6.2 |
| 9 分块 + 奇数尾字节 | §7.1-§7.3 |
| 10 缓冲有界 | §4.2、§8.1（14400 B 为各缓冲独立上界之和，非同一瞬间峰值） |
| 11 结束/排空/错误 | §9.1-§9.5 |

### 12.4 本轮修订条目对照

| 本轮修订条目 | 落实章节 |
| --- | --- |
| 两套 generation 独立、禁止相互比较 | §1、§3.2、§6.2.2 要点 3、§7.2、§7.3 |
| 会话令牌快照（`pcm_session_pcm_gen_` / `pcm_session_playback_gen_`） | §3.1、§4.2、§6.2.1 |
| 检查与 `push_back` 位于同一 `audio_queue_mutex_` 临界区 | §6.2.2、§6.4.1 |
| 软取消路线 B（含 3 帧残留上界） | §6.4、§9.3 |
| PCM 会话独占播放契约 | §9.5、§10.3 |
| 媒体时间只在成功入队后推进 | §6.2.3、§7.3、§10.4-2 |
| 完整帧 1440 / 尾帧可小于 1440 的统一描述 | §4.3、§5.3、§6.1、§12.2 |
| 14.4 KB 仅作为"独立上界之和"（≠ 同一瞬间峰值，≠ 总内存） | §4.3、§8.1、§12.2 |
| End 排空等待期间识别取消（三条返回路径） | §3.1、§9.1、§9.4、§12.2 |
| End 失败后 `pcm_stream_active_` 一律为 `false`，无需再 Abort | §9.1、§9.4、§3.1 |
| `WritePcmData()` 空块顺序修正 + 短块会话令牌补检 | §7.2、§3.3、§12.2 |
| Write 与 Abort 并发：不承诺"返回前无窗口" | §7.2、§3.3、§6.3 |
| Begin/End/Abort 控制面串行化（禁止 Begin‖Abort） | §6.2.1、§6.3、§3.3 |
| 两套代际的原子性**分别**陈述（单向保证） | §6.2.2-1、§6.4.1、§6.4.2 |

## 13. 已知取舍与后续可选优化

1. **采样率必须由服务端保证**：`aue=4` + `sampling_rate=24000` 是硬前提。
   若百度将默认值改为其它采样率，设备端会直接拒绝播放并报错（这是刻意的"显性失败"），
   需要在上层请求参数里显式固定，而不是依赖服务器默认值。
2. **不再有重采样器开销**：无 open/close、无 scratch 缓冲、无插值相位滞留，
   尾帧可直接推送；代价是失去对非 24 kHz 音源的兼容性（见 §5.1）。
3. **不影响 STEREO**：当前板卡单声道；若未来出现双声道音源，
   需同时放宽 §5.1 的 `input_channels == 1` 校验，并把帧化粒度改为
   `1440 * channels * 2` 字节、`AudioTask.pcm.size()` 相应变为 `1440 * channels`。
4. **只提供软取消**：`AbortPcmPlayback()` 之后最多再播 3 帧 ≈ 180 ms（§6.4.3），
   已入队帧不会被撤回，也不为此引入来源标记。需要立刻静音时，由上层调用既有
   `Stop()` / `ResetDecoder()`（残留 1 帧 ≈ 60 ms，但同时影响 Opus，属上层决策）。
5. **不改变 `debug_statistics_` 结构**：PCM 帧计入 `playback_count`。
   若后续需要区分，可在实现阶段单独加 `pcm_frame_count`（属追加字段，不影响 Opus）。
6. **分帧时长与打断响应的取舍**：当前 60 ms（= `OPUS_FRAME_DURATION_MS`），
   队列 2 帧 + in-flight 1 帧 ⇒ 取消后最多约 180 ms 余音。
   若实测发生欠载，可增大分帧时长，但**设计上限 100 ms**：
   分帧越长，已入队音频自然播完所需时间越长、打断响应越慢，
   `on_playback_progress` 粒度也越粗。不得为缓解欠载无限增大分帧时长；
   本轮不改变 60 ms 设计。
7. **独占播放契约的代价**：`Application` 必须显式仲裁 PCM 与 Opus（§9.5），
   否则 `EndPcmPlayback()` 的返回时间不可预测。这是"共用播放队列 + 全局排空判定"
   在不引入来源标记前提下的必然结果，不是可选项。
