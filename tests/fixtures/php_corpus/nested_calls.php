<?php
function outer($a) {
    function inner($b) {
        return $b + 1;
    }
    return inner($a);
}
