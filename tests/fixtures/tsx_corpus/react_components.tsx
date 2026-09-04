import React, { memo, forwardRef } from "react";

function Plain(): JSX.Element {
  return <div>hi</div>;
}

const Named = memo(() => {
  return <span>x</span>;
});

const Fwd = forwardRef<HTMLDivElement, {}>((props, ref) => {
  return <div ref={ref} />;
});

export default function DefaultOne(): JSX.Element {
  return <p>1</p>;
}
