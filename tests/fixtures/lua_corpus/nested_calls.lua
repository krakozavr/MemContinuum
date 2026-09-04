function outer(a)
  local function inner(b)
    return b + 1
  end
  return inner(a)
end
