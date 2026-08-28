/**
 * TrackAttemptRecorder: per-session record of which video paths have had a
 * track_suspects call initiated(调用开始即记,不管成败——成功/业务失败/
 * 工具服务错误都算已发起)。
 *
 * 用途:submit_detection 的防跳跟踪软闸门——事件 1/2/8(违停/应急车道/
 * 逆行)任一 detected=true 前,必须先对同一视频发起过 track_suspects,
 * 未发起则拒绝提交并给出指引。视频路径以统一 resolver(resolveWorkspacePath)
 * 规范化后的值为键,与工具侧一致。
 *
 * 接线:registerBuiltinTools 按 session 构造一个,同时注入 track_suspects
 * 与 submit_detection 两个工具工厂;spawn_subagent 复用 parentRegistry(同一
 * 批工具实例),子代理与父代理自然共享同一 recorder。recorder 可选注入:
 * 缺省时不启用闸门(直接构造工厂的测试用例行为不变)。
 */
export class TrackAttemptRecorder {
  private readonly attempted = new Set<string>();

  /** 记录一次 track_suspects 发起(调用开始即记,不管成败)。 */
  record(videoPath: string): void {
    this.attempted.add(videoPath);
  }

  /** 该视频路径是否已发起过 track_suspects(含失败)。 */
  hasAttempted(videoPath: string): boolean {
    return this.attempted.has(videoPath);
  }
}
