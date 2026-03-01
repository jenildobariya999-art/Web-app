let users = {};
let currentUser = null;

function login(){
  let name = document.getElementById("username").value;
  if(!users[name]){
    users[name] = {balance:0};
  }
  currentUser = name;
  document.getElementById("loginBox").style.display="none";
  document.getElementById("dashboard").style.display="block";
  updateBalance();
}

function updateBalance(){
  document.getElementById("balance").innerText = "₹"+users[currentUser].balance;
}

function showSend(){ toggle("sendBox"); }
function showWithdraw(){ toggle("withdrawBox"); }
function showDeposit(){ toggle("depositBox"); }
function showAdmin(){ toggle("adminBox"); }

function toggle(id){
  document.querySelectorAll(".hidden").forEach(e=>e.style.display="none");
  document.getElementById(id).style.display="block";
}

function payUser(){
  let receiver = document.getElementById("payUser").value;
  let amt = parseInt(document.getElementById("payAmount").value);

  if(users[currentUser].balance >= amt){
    if(!users[receiver]) users[receiver]={balance:0};
    users[currentUser].balance -= amt;
    users[receiver].balance += amt;
    alert("Payment Successful");
    updateBalance();
  }else{
    alert("Insufficient Balance");
  }
}

function withdraw(){
  let amt = parseInt(document.getElementById("withdrawAmount").value);
  if(users[currentUser].balance >= amt){
    users[currentUser].balance -= amt;
    alert("Withdraw Request Submitted");
    updateBalance();
  }else{
    alert("Insufficient Balance");
  }
}

function deposit(){
  alert("Deposit Request Sent (Admin Approval Needed)");
}

function addBalance(){
  let user = document.getElementById("adminUser").value;
  let amt = parseInt(document.getElementById("adminAmount").value);
  if(!users[user]) users[user]={balance:0};
  users[user].balance += amt;
  alert("Balance Added");
}
