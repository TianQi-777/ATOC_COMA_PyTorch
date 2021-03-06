import sys

import torch
import torch.nn as nn
from torch.optim import Adam
from torch.autograd import Variable
import torch.nn.functional as F
import os


def soft_update(target, source, tau):
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.data.copy_(target_param.data * (1.0 - tau) + param.data * tau)


def hard_update(target, source):
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.data.copy_(param.data)


# My previous implementation of DDPG was slightly different. See the repo
class ActorPart1(nn.Module):
    # The return will be the same size as the hidden_size
    # Status: Done, optimize later
    def __init__(self, hidden_size, num_inputs):
        super(ActorPart1, self).__init__()
        self.linear1 = nn.Linear(num_inputs, hidden_size)
        self.ln1 = nn.LayerNorm(hidden_size)

        self.linear2 = nn.Linear(hidden_size, hidden_size)
        self.ln2 = nn.LayerNorm(hidden_size)

    def forward(self, observation):
        x = observation
        x = self.linear1(x)
        x = self.ln1(x)
        x = F.relu(x)
        x = self.linear2(x)
        x = self.ln2(x)
        return x
        # returns "individual thought", size same as hidden_size, since this will go into the Attentional Unit


class AttentionUnit(nn.Module):
    # Currently using RNN, later try LSTM
    # ref: https://pytorch.org/tutorials/intermediate/char_rnn_classification_tutorial.html
    # ref for LSTM: https://github.com/MorvanZhou/PyTorch-Tutorial/blob/master/tutorial-contents/402_RNN_classifier.py
    """
    We assume a fixed communication bandwidth, which means each initiator can select at most m collaborators.
    The initiator first chooses collaborators from agents who have not been selected,
    then from agents selected by other initiators, Finally from other initiators, all based on
    proximity. "based on proximity" is the answer.
    """
    def __init__(self, hidden_size, num_inputs):
        # num_inputs is for the size of "thoughts"
        # num_output is binary
        super(AttentionUnit, self).__init__()
        self.hidden_size = hidden_size
        num_output = 1
        self.i2h = nn.Linear(num_inputs + hidden_size, hidden_size)
        self.i20 = nn.Linear(num_inputs + hidden_size, num_output)
        self.sigmoid = nn.Sigmoid()

    def forward(self, thoughts, hidden):  # thoughts is the output of actor_part1
        combined = torch.cat((thoughts, hidden), 1)
        hidden = self.i2h(combined)  # update the hidden state for next time-step
        output = self.i20(combined)
        output = self.sigmoid(output)
        return output, hidden

    def initHidden(self):
        return torch.zeros(1, self.hidden_size)  # maybe also try random initialization


class ActorPart2(nn.Module):
    def __init__(self, hidden_size, num_inputs, action_space):
        super(ActorPart2, self).__init__()
        self.action_space = action_space
        num_outputs = action_space.shape[0]

        self.linear1 = nn.Linear(num_inputs, hidden_size)
        self.ln1 = nn.LayerNorm(hidden_size)

        self.linear2 = nn.Linear(hidden_size, hidden_size)
        self.ln2 = nn.LayerNorm(hidden_size)

        self.mu = nn.Linear(hidden_size, num_outputs)
        self.mu.weight.data.mul_(0.1)
        self.mu.bias.data.mul_(0.1)

        self.softmax = F.softmax(num_outputs, dim=0)

    def forward(self, inputs):
        x = inputs
        x = self.linear1(x)
        x = self.ln1(x)
        x = F.relu(x)
        x = self.linear2(x)
        x = self.ln2(x)
        x = F.relu(x)
        mu = F.tanh(self.mu(x))
        output = self.softmax(mu)
        return output
        # This is the softmax probabilities for the actions of the agent


class Critic(nn.Module):
    def __init__(self, hidden_size, num_inputs, action_space):
        super(Critic, self).__init__()
        self.action_space = action_space
        num_outputs = action_space.shape[0]

        self.linear1 = nn.Linear(num_inputs, hidden_size)
        self.ln1 = nn.LayerNorm(hidden_size)

        self.linear2 = nn.Linear(hidden_size + num_outputs, hidden_size)
        # I think this is because on the second layer of critic, we concatenate the observation and the actor's action,
        # and the observation space
        # TODO: What's the reason behind this and can we do better?
        self.ln2 = nn.LayerNorm(hidden_size)

        self.V = nn.Linear(hidden_size, 1)  # This is the Q value with NN as function approximator
        self.V.weight.data.mul_(0.1)
        self.V.bias.data.mul_(0.1)

    def forward(self, inputs, actions):
        x = inputs
        x = self.linear1(x)
        x = self.ln1(x)
        x = F.relu(x)

        x = torch.cat((x, actions), 1)
        x = self.linear2(x)
        x = self.ln2(x)
        x = F.relu(x)
        V = self.V(x)
        return V


class ATOC_COMA_trainer(object):
    def __init__(self, gamma, tau, hidden_size, num_inputs, action_space):

        self.num_inputs = num_inputs
        self.action_space = action_space

        # Define actor part 1
        self.actor_p1 = ActorPart1(hidden_size, self.num_inputs)
        self.actor_target_p1 = ActorPart1(hidden_size, self.num_inputs)
        #self.actor_perturbed_p1 = ActorPart1(hidden_size, self.num_inputs)  #TODO: What is this for?
        self.actor_optim_p1 = Adam(self.actor_p1.parameters(), lr=1e-4)

        # Define actor part 2
        self.actor_p2 = ActorPart2(hidden_size, self.num_inputs, self.action_space)
        self.actor_target_p2 = ActorPart2(hidden_size, self.num_inputs, self.action_space)
        #self.actor_perturbed_p2 = ActorPart2(hidden_size, self.num_inputs, self.action_space)  #TODO: What is this for?
        self.actor_optim_p2 = Adam(self.actor_p2.parameters(), lr=1e-4)

        self.critic = Critic(hidden_size, self.num_inputs, self.action_space)
        self.critic_target = Critic(hidden_size, self.num_inputs, self.action_space)
        self.critic_optim = Adam(self.critic.parameters(), lr=1e-3)

        self.gamma = gamma
        self.tau = tau

        hard_update(self.actor_target_p1, self.actor_p1)
        hard_update(self.actor_target_p2, self.actor_p2)  # Make sure target is with the same weight
        hard_update(self.critic_target, self.critic)

    def select_action(self, state, action_noise=None, param_noise=None):
        '''
        Here, I am first trying to have the algorithm working without the attentional communication unit.
        I want to make sure the split actor 1 and actor 2 gradients are calculated correctly, and figure out how to
        share actor policy parameters
        '''
        # TODO: This needs an overhaul since here the attention and communication modules come in
        # TODO: First make it work without the attentional and communication units
        self.actor_p1.eval()  # setting the actor in evaluation mode
        self.actor_p2.eval()

        # TODO: Originally there was parameter noise incorporated, using the actor_purturbed, revisit original code
        actor1_action = self.actor_p1((Variable(state)))  # this gets us the thoughts
        actor2_action = self.actor_p2(Variable(actor1_action))  # directly passing thoughts to actor2

        self.actor_p1.train()
        self.actor_p2.train()
        final_action = actor2_action.data

        if action_noise is not None:
            final_action += torch.Tensor(action_noise.noise())

        return final_action.clamp(-1, 1)  # TODO: revisit the theory behind clamping/clipping

    def update_parameters(self, batch):
        # TODO: How to update (get gradients for) actor_part1. I think the dynamic graph should update itself
        # TODO: understand how they batches are working. Currently I am assuming they work as they should
        state_batch = Variable(torch.cat(batch.state))
        action_batch = Variable(torch.cat(batch.action))
        reward_batch = Variable(torch.cat(batch.reward))
        mask_batch = Variable(torch.cat(batch.mask))  # TODO: What is this mask?
        next_state_batch = Variable(torch.cat(batch.next_state))

        next_action_batch = self.actor_target(next_state_batch)
        next_state_action_values = self.critic_target(next_state_batch, next_action_batch)

        reward_batch = reward_batch.unsqueeze(1)
        mask_batch = mask_batch.unsqueeze(1)
        expected_state_action_batch = reward_batch + (self.gamma * mask_batch * next_state_action_values)

        self.critic_optim.zero_grad()

        state_action_batch = self.critic((state_batch), (action_batch))

        value_loss = F.mse_loss(state_action_batch, expected_state_action_batch)
        value_loss.backward()
        self.critic_optim.step()

        # TODO: I NEED TO MAKE CHANGES HERE. EVERYTHING ABOVE SEEMS TO BE FINE
        self.actor_optim.zero_grad()

        policy_loss = -self.critic((state_batch), self.actor((state_batch)))

        policy_loss = policy_loss.mean()
        policy_loss.backward()
        self.actor_optim.step()

        soft_update(self.actor_target, self.actor, self.tau)
        soft_update(self.critic_target, self.critic, self.tau)

        return value_loss.item(), policy_loss.item()

    def perturb_actor_parameters(self, param_noise):
        """Apply parameter noise to actor model, for exploration"""
        hard_update(self.actor_perturbed, self.actor)
        params = self.actor_perturbed.state_dict()
        for name in params:
            if 'ln' in name:
                pass
            param = params[name]
            param += torch.randn(param.shape) * param_noise.current_stddev

    def save_model(self, env_name, suffix="", actor_path=None, critic_path=None):
        if not os.path.exists('models/'):
            os.makedirs('models/')

        if actor_path is None:
            actor_path = "models/ddpg_actor_{}_{}".format(env_name, suffix)
        if critic_path is None:
            critic_path = "models/ddpg_critic_{}_{}".format(env_name, suffix)
        print('Saving models to {} and {}'.format(actor_path, critic_path))
        torch.save(self.actor.state_dict(), actor_path)
        torch.save(self.critic.state_dict(), critic_path)

    def load_model(self, actor_path, critic_path):
        print('Loading models from {} and {}'.format(actor_path, critic_path))
        if actor_path is not None:
            self.actor.load_state_dict(torch.load(actor_path))
        if critic_path is not None:
            self.critic.load_state_dict(torch.load(critic_path))
