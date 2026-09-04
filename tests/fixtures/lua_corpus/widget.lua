function plain(a)
  return a + 1
end

local function loc(a)
  return a + 2
end

-- Overwrites the widget's f field.
M.f = function(a)
  return a
end

function M.g(a)
  return a
end

function obj:m(a)
  return a
end
