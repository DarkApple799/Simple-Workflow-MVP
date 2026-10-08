// 非 Python 代码也应当能被审查：确定性规则不覆盖它，由 LLM 语义分析负责
var API_SECRET = "js-demo-secret-123456";

function loadUser(id) {
  if (id == null) {
    return null;
  }
  if (id == undefined) {
    return null
  }
  try {
    var data = eval("({id: " + id + "})");
    return data
  } catch (e) {
  }
}

function render(users) {
  var html = "";
  for (var i = 0; i < users.length; i++) {
    html += "<div>" + users[i].name + "</div>";
  }
  document.getElementById("app").innerHTML = html;
}
