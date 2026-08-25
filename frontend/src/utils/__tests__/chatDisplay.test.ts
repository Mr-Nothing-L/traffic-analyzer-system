// 对话视图展示纯函数测试:
// - shouldSendOnEnter:输入法合成态(isComposing/keyCode 229)与 Shift+Enter 不发送;
// - toolLabel:工具名中文映射,未知工具回退原名;
// - workspaceVideoSrc:气泡视频地址由 path 确定性推导(历史重载同源)。
import { describe, it, expect } from 'vitest';
import { shouldSendOnEnter, toolLabel, workspaceVideoSrc } from '../chatDisplay';

describe('shouldSendOnEnter(composer Enter 发送判定)', () => {
  it('普通 Enter 发送', () => {
    expect(shouldSendOnEnter({ shiftKey: false })).toBe(true);
  });

  it('Shift+Enter 换行,不发送', () => {
    expect(shouldSendOnEnter({ shiftKey: true })).toBe(false);
  });

  it('输入法合成态(isComposing)的 Enter 是选词上屏,不发送', () => {
    expect(shouldSendOnEnter({ shiftKey: false, isComposing: true })).toBe(false);
  });

  it('keyCode 229(部分浏览器合成态只给 229)不发送', () => {
    expect(shouldSendOnEnter({ shiftKey: false, keyCode: 229 })).toBe(false);
  });
});

describe('toolLabel(工具名中文映射)', () => {
  it('已知工具显示「中文名(原名)」', () => {
    expect(toolLabel('video_meta')).toBe('视频元信息(video_meta)');
    expect(toolLabel('extract_frames')).toBe('抽帧(extract_frames)');
    expect(toolLabel('draw_boxes')).toBe('画框标注(draw_boxes)');
    expect(toolLabel('read_file')).toBe('读文件(read_file)');
    expect(toolLabel('write_file')).toBe('写文件(write_file)');
    expect(toolLabel('run_script')).toBe('运行脚本(run_script)');
    expect(toolLabel('submit_detection')).toBe('提交检测结果(submit_detection)');
  });

  it('未知工具回退原名', () => {
    expect(toolLabel('some_new_tool')).toBe('some_new_tool');
  });
});

describe('workspaceVideoSrc(气泡视频预览地址推导)', () => {
  it('有 src(当次上传附件)直接用', () => {
    expect(workspaceVideoSrc('a/b.mp4', '/api/agent/uploads/x.mp4', '/ws')).toBe(
      '/api/agent/uploads/x.mp4',
    );
  });

  it('工作区相对路径推 /api/workspace/stream(同路径恒同地址)', () => {
    expect(workspaceVideoSrc('sub/视频 1.mp4', undefined, '/ws')).toBe(
      `/api/workspace/stream?path=${encodeURIComponent('sub/视频 1.mp4')}`,
    );
  });

  it('工作区内绝对路径(如 .agent/uploads 落盘)剥前缀转相对', () => {
    expect(
      workspaceVideoSrc('/ws/.agent/uploads/20240101_a.mp4', undefined, '/ws'),
    ).toBe(`/api/workspace/stream?path=${encodeURIComponent('.agent/uploads/20240101_a.mp4')}`);
  });

  it('工作区外绝对路径推不出,返回 null(回退路径 chip)', () => {
    expect(workspaceVideoSrc('/other/x.mp4', undefined, '/ws')).toBeNull();
  });

  it('无 path 返回 null', () => {
    expect(workspaceVideoSrc(undefined, undefined, '/ws')).toBeNull();
  });
});
