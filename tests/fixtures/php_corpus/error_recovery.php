<?php
function good($a) {
    return $a + 1;
}

function broken($c) {
    return @@@;
}

function alsoGood($b) {
    return $b * 2;
}
