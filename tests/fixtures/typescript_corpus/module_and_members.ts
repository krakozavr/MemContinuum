module Shapes {
  export function area(): number {
    return 1;
  }
}

namespace Solids {
  export function volume(): number {
    return 2;
  }
}

function* ids(): Generator<number> {
  yield 1;
}

class Box {
  #secret(): number {
    return 1;
  }

  private hidden(): number {
    return 2;
  }
}

const api = {
  get(): number {
    return 3;
  },
};
