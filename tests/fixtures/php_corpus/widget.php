<html>
<body>
<?php
function top_level($a) {
    return $a + 1;
}

class Widget {
    public function __construct($x) {
        $this->x = $x;
    }

    public function render() {
        return $this->x;
    }
}

trait Greets {
    public function greet() {
        return "hi";
    }
}
?>
</body>
</html>
