import React, { memo, forwardRef } from "react";

const Foo = memo(() => {
  return null;
});

const Bar = forwardRef(function BarImpl(props, ref) {
  return null;
});

export default memo(() => {
  return null;
});
