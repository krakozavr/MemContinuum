import { memo } from "react";

const Boxed = memo(function (props: { x: number }) {
  return <div>{props.x}</div>;
});
