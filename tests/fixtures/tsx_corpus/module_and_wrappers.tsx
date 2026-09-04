module Shapes {
  export function area(): number {
    return 1;
  }
}

const Memoized = React.memo(() => {
  return null;
});

class Box {
  #secret(): number {
    return 1;
  }
}

declare module "vendor-lib" {
  export function shim(): number {
    return 4;
  }
}
