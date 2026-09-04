function Good(): JSX.Element {
  return <div>ok</div>;
}

function Broken(): JSX.Element {
  return @@@;
}

function AlsoGood(): JSX.Element {
  return <span>ok</span>;
}
