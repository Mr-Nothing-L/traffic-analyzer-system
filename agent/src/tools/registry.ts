import type { ExecutableTool } from './contract';

/** Map-based tool registry (no DI container). */
export class ToolRegistry {
  private readonly tools = new Map<string, ExecutableTool>();

  register(tool: ExecutableTool): void {
    if (this.tools.has(tool.name)) {
      throw new Error(`Tool "${tool.name}" is already registered`);
    }
    this.tools.set(tool.name, tool);
  }

  list(): ExecutableTool[] {
    return [...this.tools.values()];
  }

  resolve(name: string): ExecutableTool | undefined {
    return this.tools.get(name);
  }
}
