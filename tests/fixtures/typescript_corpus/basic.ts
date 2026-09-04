function plain(a: number, b: number): number {
  return a + b;
}

const arrowed = (x: number): number => x * 2;

interface Greeter {
  greet(): string;
}

function overloaded(x: number): number;
function overloaded(x: string): string;
function overloaded(x: any): any {
  return x;
}

namespace Util {
  export function helper(): number {
    return 42;
  }
}

class Widget {
  constructor(private x: number) {}

  get value(): number {
    return this.x;
  }
}
